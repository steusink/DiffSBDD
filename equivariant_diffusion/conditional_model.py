import math

import numpy as np
import torch
import torch.nn.functional as F
from torch_scatter import scatter_add, scatter_mean

import utils
from equivariant_diffusion.en_diffusion import EnVariationalDiffusion


class ConditionalDDPM(EnVariationalDiffusion):
    """
    Conditional Diffusion Module.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        assert not self.dynamics.update_pocket_coords
        

    def kl_prior(self, xh_lig, mask_lig, num_nodes):
        """Computes the KL between q(z1 | x) and the prior p(z1) = Normal(0, 1).

        This is essentially a lot of work for something that is in practice
        negligible in the loss. However, you compute it so that you see it when
        you've made a mistake in your noise schedule.
        """
        batch_size = len(num_nodes)

        # Compute the last alpha value, alpha_T.
        ones = torch.ones((batch_size, 1), device=xh_lig.device)
        gamma_T = self.gamma(ones)
        alpha_T = self.alpha(gamma_T, xh_lig)

        # Compute means.
        mu_T_lig = alpha_T[mask_lig] * xh_lig
        mu_T_lig_x, mu_T_lig_h = mu_T_lig[:, : self.n_dims], mu_T_lig[:, self.n_dims :]

        # Compute standard deviations (only batch axis for x-part, inflated for h-part).
        sigma_T_x = self.sigma(gamma_T, mu_T_lig_x).squeeze()
        sigma_T_h = self.sigma(gamma_T, mu_T_lig_h).squeeze()

        # Compute KL for h-part.
        zeros = torch.zeros_like(mu_T_lig_h)
        ones = torch.ones_like(sigma_T_h)
        mu_norm2 = self.sum_except_batch((mu_T_lig_h - zeros) ** 2, mask_lig)
        kl_distance_h = self.gaussian_KL(mu_norm2, sigma_T_h, ones, d=1)

        # Compute KL for x-part.
        zeros = torch.zeros_like(mu_T_lig_x)
        ones = torch.ones_like(sigma_T_x)
        mu_norm2 = self.sum_except_batch((mu_T_lig_x - zeros) ** 2, mask_lig)
        subspace_d = self.subspace_dimensionality(num_nodes)
        kl_distance_x = self.gaussian_KL(mu_norm2, sigma_T_x, ones, subspace_d)

        return kl_distance_x + kl_distance_h

    def log_pxh_given_z0_without_constants(
        self, ligand, z_0_lig, eps_lig, net_out_lig, gamma_0, epsilon=1e-10
    ):

        # Discrete properties are predicted directly from z_t.
        z_h_lig = z_0_lig[:, self.n_dims :]

        # Take only part over x.
        eps_lig_x = eps_lig[:, : self.n_dims]
        net_lig_x = net_out_lig[:, : self.n_dims]

        # Compute sigma_0 and rescale to the integer scale of the data.
        sigma_0 = self.sigma(gamma_0, target_tensor=z_0_lig)
        sigma_0_cat = sigma_0 * self.norm_values[1]

        # Computes the error for the distribution
        # N(x | 1 / alpha_0 z_0 + sigma_0/alpha_0 eps_0, sigma_0 / alpha_0),
        # the weighting in the epsilon parametrization is exactly '1'.
        log_p_x_given_z0_without_constants_ligand = -0.5 * (
            self.sum_except_batch((eps_lig_x - net_lig_x) ** 2, ligand["mask"])
        )

        # Compute delta indicator masks.
        # un-normalize
        ligand_onehot = ligand["one_hot"] * self.norm_values[1] + self.norm_biases[1]

        estimated_ligand_onehot = z_h_lig * self.norm_values[1] + self.norm_biases[1]

        # Centered h_cat around 1, since onehot encoded.
        centered_ligand_onehot = estimated_ligand_onehot - 1

        # Compute integrals from 0.5 to 1.5 of the normal distribution
        # N(mean=z_h_cat, stdev=sigma_0_cat)
        log_ph_cat_proportional_ligand = torch.log(
            self.cdf_standard_gaussian(
                (centered_ligand_onehot + 0.5) / sigma_0_cat[ligand["mask"]]
            )
            - self.cdf_standard_gaussian(
                (centered_ligand_onehot - 0.5) / sigma_0_cat[ligand["mask"]]
            )
            + epsilon
        )

        # Normalize the distribution over the categories.
        log_Z = torch.logsumexp(log_ph_cat_proportional_ligand, dim=1, keepdim=True)
        log_probabilities_ligand = log_ph_cat_proportional_ligand - log_Z

        # Select the log_prob of the current category using the onehot
        # representation.
        log_ph_given_z0_ligand = self.sum_except_batch(
            log_probabilities_ligand * ligand_onehot, ligand["mask"]
        )

        return log_p_x_given_z0_without_constants_ligand, log_ph_given_z0_ligand

    def sample_p_xh_given_z0(
        self,
        z0_lig,
        xh0_pocket,
        lig_mask,
        pocket_mask,
        batch_size,
        fix_noise=False,
        guided=False,
        gradient_scale=1.0,
    ):
        """Samples x ~ p(x|z0)."""
        t_zeros = torch.zeros(size=(batch_size, 1), device=z0_lig.device)
        gamma_0 = self.gamma(t_zeros)
        # Computes sqrt(sigma_0^2 / alpha_0^2)
        sigma_x = self.SNR(-0.5 * gamma_0)
        # net_out_lig, _ = self.dynamics(
        #     z0_lig, xh0_pocket, t_zeros, lig_mask, pocket_mask
        # )
        # # changes made here
        # net_out_lig_x = net_out_lig[:, : self.n_dims]
        net_out_lig_x = self.dynamics(
            z0_lig, xh0_pocket, t_zeros, lig_mask, pocket_mask
        )

        z0_lig_x = z0_lig[:, : self.n_dims]

        # Compute mu for p(zs | zt).
        mu_x_lig = self.compute_x_pred(net_out_lig_x, z0_lig_x, gamma_0, lig_mask)

        if guided:
            de_d_lig_x = self.energy_guider.get_energy_gradient(
                z0_lig, xh0_pocket, lig_mask, pocket_mask
            )
            mu_lig_x = mu_lig_x - gradient_scale * de_d_lig_x * sigma_x

        x_lig, xh0_pocket = self.sample_normal_zero_com(
            mu_x_lig, xh0_pocket, sigma_x, lig_mask, pocket_mask, fix_noise
        )

        x_lig, h_lig = self.unnormalize(x_lig, z0_lig[:, self.n_dims :])
        x_pocket, h_pocket = self.unnormalize(
            xh0_pocket[:, : self.n_dims], xh0_pocket[:, self.n_dims :]
        )

        h_lig = F.one_hot(torch.argmax(h_lig, dim=1), self.atom_nf)
        # h_pocket = F.one_hot(torch.argmax(h_pocket, dim=1), self.residue_nf)

        return x_lig, h_lig, x_pocket, h_pocket

    def sample_normal(self, *args):
        raise NotImplementedError("Has been replaced by sample_normal_zero_com()")

    def sample_normal_zero_com(
        self, mu_lig_x, xh0_pocket, sigma, lig_mask, pocket_mask, fix_noise=False
    ):
        """Samples from a Normal distribution."""
        if fix_noise:
            # bs = 1 if fix_noise else mu.size(0)
            raise NotImplementedError("fix_noise option isn't implemented yet")

        # changes made for peptides!
        # eps_lig = self.sample_gaussian(
        #     size=(len(lig_mask), self.n_dims + self.atom_nf), device=lig_mask.device
        # )
        eps_lig = self.sample_gaussian(
            size=(len(lig_mask), self.n_dims), device=lig_mask.device
        )

        out_lig_x = mu_lig_x + sigma[lig_mask] * eps_lig

        # project to COM-free subspace
        xh_pocket = xh0_pocket.detach().clone()
        # out_lig[:, : self.n_dims], xh_pocket[:, : self.n_dims] = self.remove_mean_batch(
        #     out_lig[:, : self.n_dims],
        #     xh0_pocket[:, : self.n_dims],
        #     lig_mask,
        #     pocket_mask,
        # )

        out_lig_x, xh_pocket[:, : self.n_dims] = self.remove_mean_batch(
            out_lig_x,
            xh0_pocket[:, : self.n_dims],
            lig_mask,
            pocket_mask,
        )

        return out_lig_x, xh_pocket

    def noised_representation(self, xh_lig, xh0_pocket, lig_mask, pocket_mask, gamma_t):
        # peptide changes made!

        # Compute alpha_t and sigma_t from gamma.
        alpha_t = self.alpha(gamma_t, xh_lig)
        sigma_t = self.sigma(gamma_t, xh_lig)

        # Sample zt ~ Normal(alpha_t x, sigma_t)
        # eps_lig = self.sample_gaussian(
        #     size=(len(lig_mask), self.n_dims + self.atom_nf), device=lig_mask.device
        # )
        eps_lig = self.sample_gaussian(
            size=(len(lig_mask), self.n_dims), device=lig_mask.device
        )

        # Sample z_t given x, h for timestep t, from q(z_t | x, h)
        xz_t_lig = (
            alpha_t[lig_mask] * xh_lig[:, : self.n_dims] + sigma_t[lig_mask] * eps_lig
        )
        z_t_lig = torch.cat((xz_t_lig, xh_lig[:, self.n_dims :]), dim=1)

        # project to COM-free subspace
        xh_pocket = xh0_pocket.detach().clone()
        z_t_lig[:, : self.n_dims], xh_pocket[:, : self.n_dims] = self.remove_mean_batch(
            z_t_lig[:, : self.n_dims],
            xh_pocket[:, : self.n_dims],
            lig_mask,
            pocket_mask,
        )

        return z_t_lig, xh_pocket, eps_lig

    def log_pN(self, N_lig, N_pocket):
        """
        Prior on the sample size for computing
        log p(x,h,N) = log p(x,h|N) + log p(N), where log p(x,h|N) is the
        model's output
        Args:
            N: array of sample sizes
        Returns:
            log p(N)
        """
        log_pN = self.size_distribution.log_prob_n1_given_n2(N_lig, N_pocket)
        return log_pN

    def delta_log_px(self, num_nodes):
        return -self.subspace_dimensionality(num_nodes) * np.log(self.norm_values[0])

    def forward(self, ligand, pocket, return_info=False):
        """
        Computes the loss and NLL terms
        """
        # Normalize data, take into account volume change in x.
        ligand, pocket = self.normalize(ligand, pocket)

        # Likelihood change due to normalization
        delta_log_px = self.delta_log_px(ligand["size"])

        # Sample a timestep t for each example in batch
        # At evaluation time, loss_0 will be computed separately to decrease
        # variance in the estimator (costs two forward passes)
        lowest_t = 0 if self.training else 1
        t_int = torch.randint(
            lowest_t,
            self.T + 1,
            size=(ligand["size"].size(0), 1),
            device=ligand["x"].device,
        ).float()
        s_int = t_int - 1  # previous timestep

        # Masks: important to compute log p(x | z0).
        t_is_zero = (t_int == 0).float()
        t_is_not_zero = 1 - t_is_zero

        # Normalize t to [0, 1]. Note that the negative
        # step of s will never be used, since then p(x | z0) is computed.
        s = s_int / self.T
        t = t_int / self.T

        # Compute gamma_s and gamma_t via the network.
        gamma_s = self.inflate_batch_array(self.gamma(s), ligand["x"])
        gamma_t = self.inflate_batch_array(self.gamma(t), ligand["x"])

        # Concatenate x, and h[categorical].
        xh0_lig = torch.cat([ligand["x"], ligand["one_hot"]], dim=1)
        xh0_pocket = torch.cat([pocket["x"], pocket["one_hot"]], dim=1)

        # Center the input nodes
        (
            xh0_lig[:, : self.n_dims],
            xh0_pocket[:, : self.n_dims],
        ) = self.remove_mean_batch(
            xh0_lig[:, : self.n_dims],
            xh0_pocket[:, : self.n_dims],
            ligand["mask"],
            pocket["mask"],
        )

        # Find noised representation
        z_t_lig, xh_pocket, eps_t_lig = self.noised_representation(
            xh0_lig, xh0_pocket, ligand["mask"], pocket["mask"], gamma_t
        )

        # Neural net prediction.
        # net_xh_out_lig, _ = self.dynamics(
        #     z_t_lig, xh_pocket, t, ligand["mask"], pocket["mask"]
        # )
        # # added to only update x
        # net_out_lig = net_xh_out_lig[:, : self.n_dims]
        net_out_lig = self.dynamics(
            z_t_lig, xh_pocket, t, ligand["mask"], pocket["mask"]
        )

        # For LJ loss term
        # xh_lig_hat does not need to be zero-centered as it is only used for
        # computing relative distances
        # changed to only do x
        x_lig_hat = self.xh_given_zt_and_epsilon(
            z_t_lig, net_out_lig, gamma_t, ligand["mask"]
        )
        xh_lig_hat = torch.cat((x_lig_hat, ligand["one_hot"]), dim=1)

        # Compute the L2 error.
        error_t_lig = self.sum_except_batch(
            (eps_t_lig - net_out_lig) ** 2, ligand["mask"]
        )

        # Compute weighting with SNR: (1 - SNR(s-t)) for epsilon parametrization
        SNR_weight = (1 - self.SNR(gamma_s - gamma_t)).squeeze(1)
        assert error_t_lig.size() == SNR_weight.size()

        # The _constants_ depending on sigma_0 from the
        # cross entropy term E_q(z0 | x) [log p(x | z0)].
        neg_log_constants = -self.log_constants_p_x_given_z0(
            n_nodes=ligand["size"], device=error_t_lig.device
        )

        # The KL between q(zT | x) and p(zT) = Normal(0, 1).
        # Should be close to zero.
        kl_prior = self.kl_prior(xh0_lig, ligand["mask"], ligand["size"])

        if self.training:
            # Computes the L_0 term (even if gamma_t is not actually gamma_0)
            # and this will later be selected via masking.
            (
                log_p_x_given_z0_without_constants_ligand,
                log_ph_given_z0,
            ) = self.log_pxh_given_z0_without_constants(
                ligand, z_t_lig, eps_t_lig, net_out_lig, gamma_t
            )

            loss_0_x_ligand = (
                -log_p_x_given_z0_without_constants_ligand * t_is_zero.squeeze()
            )
            loss_0_h = -log_ph_given_z0 * t_is_zero.squeeze()

            # apply t_is_zero mask
            error_t_lig = error_t_lig * t_is_not_zero.squeeze()

        else:
            # Compute noise values for t = 0.
            t_zeros = torch.zeros_like(s)
            gamma_0 = self.inflate_batch_array(self.gamma(t_zeros), ligand["x"])

            # Sample z_0 given x, h for timestep t, from q(z_t | x, h)
            z_0_lig, xh_pocket, eps_0_lig = self.noised_representation(
                xh0_lig, xh0_pocket, ligand["mask"], pocket["mask"], gamma_0
            )

            net_out_0_lig = self.dynamics(
                z_0_lig, xh_pocket, t_zeros, ligand["mask"], pocket["mask"]
            )

            (
                log_p_x_given_z0_without_constants_ligand,
                log_ph_given_z0,
            ) = self.log_pxh_given_z0_without_constants(
                ligand, z_0_lig, eps_0_lig, net_out_0_lig, gamma_0
            )
            loss_0_x_ligand = -log_p_x_given_z0_without_constants_ligand
            loss_0_h = -log_ph_given_z0

        # sample size prior
        # log_pN = self.log_pN(ligand["size"], pocket["size"])
        # log_pN = torch.zeros((1,1), device=ligand["x"].device)

        info = {
            "eps_hat_lig_x": scatter_mean(
                net_out_lig[:, : self.n_dims].abs().mean(1), ligand["mask"], dim=0
            ).mean(),
            "eps_hat_lig_h": scatter_mean(
                net_out_lig[:, self.n_dims :].abs().mean(1), ligand["mask"], dim=0
            ).mean(),
        }
        loss_terms = (
            delta_log_px,
            error_t_lig,
            torch.tensor(0.0),
            SNR_weight,
            loss_0_x_ligand,
            torch.tensor(0.0),
            loss_0_h,
            neg_log_constants,
            kl_prior,
            # log_pN,
            torch.tensor(0.0),
            t_int.squeeze(),
            xh_lig_hat,
        )
        return (*loss_terms, info) if return_info else loss_terms

    def xh_given_zt_and_epsilon(self, z_t, epsilon, gamma_t, batch_mask):
        """Equation (7) in the EDM paper"""
        alpha_t = self.alpha(gamma_t, z_t)
        sigma_t = self.sigma(gamma_t, z_t)

        # changed to only do x
        x = (
            z_t[:, : self.n_dims] / alpha_t[batch_mask]
            - epsilon * sigma_t[batch_mask] / alpha_t[batch_mask]
        )
        return x

    def sample_p_zt_given_zs(
        self,
        zs_lig,
        xh0_pocket,
        ligand_mask,
        pocket_mask,
        gamma_t,
        gamma_s,
        fix_noise=False,
    ):
        (
            sigma2_t_given_s,
            sigma_t_given_s,
            alpha_t_given_s,
        ) = self.sigma_and_alpha_t_given_s(gamma_t, gamma_s, zs_lig)

        mu_lig = alpha_t_given_s[ligand_mask] * zs_lig
        zt_lig, xh0_pocket = self.sample_normal_zero_com(
            mu_lig, xh0_pocket, sigma_t_given_s, ligand_mask, pocket_mask, fix_noise
        )

        return zt_lig, xh0_pocket

    def sample_p_zs_given_zt(
        self,
        s,
        t,
        zt_lig,
        xh0_pocket,
        ligand_mask,
        pocket_mask,
        fix_noise=False,
        guided=False,
        gradient_scale=1.0,
        guidance_starts_at=0,
    ):
        """Samples from zs ~ p(zs | zt). Only used during sampling."""
        gamma_s = self.gamma(s)
        gamma_t = self.gamma(t)

        (
            sigma2_t_given_s,
            sigma_t_given_s,
            alpha_t_given_s,
        ) = self.sigma_and_alpha_t_given_s(gamma_t, gamma_s, zt_lig)

        sigma_s = self.sigma(gamma_s, target_tensor=zt_lig)
        sigma_t = self.sigma(gamma_t, target_tensor=zt_lig)

        # Neural net prediction.
        # changes made
        # eps_t_lig, _ = self.dynamics(zt_lig, xh0_pocket, t, ligand_mask, pocket_mask)
        # eps_t_lig_x = eps_t_lig[:, : self.n_dims]
        eps_t_lig_x = self.dynamics(zt_lig, xh0_pocket, t, ligand_mask, pocket_mask)

        # Compute mu for p(zs | zt).
        # Note: mu_{t->s} = 1 / alpha_{t|s} z_t - sigma_{t|s}^2 / sigma_t / alpha_{t|s} epsilon
        # follows from the definition of mu_{t->s} and Equ. (7) in the EDM paper
        # changes made
        mu_lig_x = (
            zt_lig[:, : self.n_dims] / alpha_t_given_s[ligand_mask]
            - (sigma2_t_given_s / alpha_t_given_s / sigma_t)[ligand_mask] * eps_t_lig_x
        )

        # Compute sigma for p(zs | zt).
        sigma = sigma_t_given_s * sigma_s / sigma_t

        # If guided, move the sampling mean towards the lower energy region.
        if guided and self.T - t >= guidance_starts_at:
            de_d_lig_x = self.energy_guider.get_energy_gradient(
                zt_lig, xh0_pocket, ligand_mask, pocket_mask
            )
            mu_lig_x = mu_lig_x - gradient_scale * de_d_lig_x * sigma

        # Sample zs given the parameters derived from zt.
        # changes made
        zs_lig_x, xh0_pocket = self.sample_normal_zero_com(
            mu_lig_x, xh0_pocket, sigma, ligand_mask, pocket_mask, fix_noise
        )
        zs_lig = torch.cat((zs_lig_x, zt_lig[:, self.n_dims :]), dim=1)

        self.assert_mean_zero_with_mask(zt_lig[:, : self.n_dims], ligand_mask)

        return zs_lig, xh0_pocket

    def sample_combined_position_feature_noise(
        self, lig_indices, xh0_pocket, pocket_indices
    ):
        """
        Samples mean-centered normal noise for z_x, and standard normal noise
        for z_h.
        """
        raise NotImplementedError("Use sample_normal_zero_com() instead.")

    def sample(self, *args):
        raise NotImplementedError(
            "Conditional model does not support sampling " "without given pocket."
        )

    @torch.no_grad()
    def sample_given_pocket(
        self,
        pocket,
        lig_one_hot,
        lig_mask,
        return_frames=1,
        timesteps=None,
        guided=False,
        gradient_scale=1.0,
        guidance_starts_at=0,
    ):
        """
        Draw samples from the generative model. Optionally, return intermediate
        states for visualization purposes.
        """
        timesteps = self.T if timesteps is None else timesteps
        assert 0 < return_frames <= timesteps
        assert timesteps % return_frames == 0

        n_samples = len(pocket["size"])
        device = pocket["x"].device

        lig, pocket = self.normalize(
            pocket=pocket, ligand={"x": 0, "one_hot": lig_one_hot}
        )

        # xh0_pocket is the original pocket while xh_pocket might be a
        # translated version of it
        xh0_pocket = torch.cat([pocket["x"], pocket["one_hot"]], dim=1)

        # lig_mask = utils.num_nodes_to_batch_mask(n_samples, num_nodes_lig, device)

        # Sample from Normal distribution in the pocket center
        mu_lig_x = scatter_mean(pocket["x"], pocket["mask"], dim=0)[lig_mask]

        # mu_lig_h = torch.zeros((n_samples, self.atom_nf), device=device)
        # mu_lig = torch.cat((mu_lig_x, mu_lig_h), dim=1)[lig_mask]
        sigma = torch.ones_like(pocket["size"]).unsqueeze(1)

        # changes to only do x
        z_lig_x, xh_pocket = self.sample_normal_zero_com(
            mu_lig_x, xh0_pocket, sigma, lig_mask, pocket["mask"]
        )
        z_lig = torch.cat((z_lig_x, lig["one_hot"]), dim=1)

        self.assert_mean_zero_with_mask(z_lig[:, : self.n_dims], lig_mask)

        out_lig = torch.zeros((return_frames,) + z_lig.size(), device=z_lig.device)
        out_pocket = torch.zeros((return_frames,) + xh_pocket.size(), device=device)

        # Iteratively sample p(z_s | z_t) for t = 1, ..., T, with s = t - 1.
        for s in reversed(range(0, timesteps)):
            s_array = torch.full((n_samples, 1), fill_value=s, device=z_lig.device)
            t_array = s_array + 1
            s_array = s_array / timesteps
            t_array = t_array / timesteps

            z_lig, xh_pocket = self.sample_p_zs_given_zt(
                s_array,
                t_array,
                z_lig,
                xh_pocket,
                lig_mask, pocket["mask"],
                guided=guided,
                gradient_scale=gradient_scale,
                guidance_starts_at=guidance_starts_at,
            )

            # save frame
            if (s * return_frames) % timesteps == 0:
                idx = (s * return_frames) // timesteps
                out_lig[idx], out_pocket[idx] = self.unnormalize_z(z_lig, xh_pocket)

        # Finally sample p(x, h | z_0).
        # changes made
        x_lig, _, x_pocket, h_pocket = self.sample_p_xh_given_z0(
            z_lig, xh_pocket, lig_mask, pocket["mask"], n_samples
        )
        h_lig = lig["one_hot"]

        self.assert_mean_zero_with_mask(x_lig, lig_mask)

        # Correct CoM drift for examples without intermediate states
        if return_frames == 1:
            max_cog = scatter_add(x_lig, lig_mask, dim=0).abs().max().item()
            if max_cog > 5e-2:
                print(
                    f"Warning CoG drift with error {max_cog:.3f}. Projecting "
                    f"the positions down."
                )
                x_lig, x_pocket = self.remove_mean_batch(
                    x_lig, x_pocket, lig_mask, pocket["mask"]
                )

        # Overwrite last frame with the resulting x and h.
        out_lig[0] = torch.cat([x_lig, h_lig], dim=1)
        out_pocket[0] = torch.cat([x_pocket, h_pocket], dim=1)

        # remove frame dimension if only the final molecule is returned
        return out_lig.squeeze(0), out_pocket.squeeze(0), lig_mask, pocket["mask"]

    @classmethod
    def remove_mean_batch(cls, x_lig, x_pocket, lig_indices, pocket_indices):

        # Just subtract the center of mass of the sampled part
        mean = scatter_mean(x_lig, lig_indices, dim=0)

        x_lig = x_lig - mean[lig_indices]
        x_pocket = x_pocket - mean[pocket_indices]
        return x_lig, x_pocket


# ------------------------------------------------------------------------------
# The same model without subspace-trick
# ------------------------------------------------------------------------------
class SimpleConditionalDDPM(ConditionalDDPM):
    """
    Simpler conditional diffusion module without subspace-trick.
    - rotational equivariance is guaranteed by construction
    - translationally equivariant likelihood is achieved by first mapping
      samples to a space where the context is COM-free and evaluating the
      likelihood there
    - molecule generation is equivariant because we can first sample in the
      space where the context is COM-free and translate the whole system back to
      the original position of the context later
    """

    def subspace_dimensionality(self, input_size):
        """Override because we don't use the linear subspace anymore."""
        return input_size * self.n_dims

    @classmethod
    def remove_mean_batch(cls, x_lig, x_pocket, lig_indices, pocket_indices):
        """Hacky way of removing the centering steps without changing too much
        code."""
        return x_lig, x_pocket

    @staticmethod
    def assert_mean_zero_with_mask(x, node_mask, eps=1e-10):
        return

    def forward(self, ligand, pocket, return_info=False):

        # Subtract pocket center of mass
        pocket_com = scatter_mean(pocket["x"], pocket["mask"], dim=0)
        ligand["x"] = ligand["x"] - pocket_com[ligand["mask"]]
        pocket["x"] = pocket["x"] - pocket_com[pocket["mask"]]

        return super(SimpleConditionalDDPM, self).forward(ligand, pocket, return_info)

    @torch.no_grad()
    def sample_given_pocket(
        self, pocket, num_nodes_lig, return_frames=1, timesteps=None
    ):

        # Subtract pocket center of mass
        pocket_com = scatter_mean(pocket["x"], pocket["mask"], dim=0)
        pocket["x"] = pocket["x"] - pocket_com[pocket["mask"]]

        return super(SimpleConditionalDDPM, self).sample_given_pocket(
            pocket, num_nodes_lig, return_frames, timesteps
        )
