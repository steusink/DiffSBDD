import math

import torch
import torch.nn as nn
from equivariant_diffusion.egnn_new import EGNN, GNN
from equivariant_diffusion.en_diffusion import EnVariationalDiffusion

remove_mean_batch = EnVariationalDiffusion.remove_mean_batch
import numpy as np
from torch_geometric.nn.encoding import PositionalEncoding


class EGNNDynamics(nn.Module):
    def __init__(
        self,
        atom_nf,
        residue_nf,
        n_dims,
        joint_nf=16,
        hidden_nf=64,
        device="cpu",
        act_fn=torch.nn.SiLU(),
        n_layers=4,
        attention=False,
        condition_time=True,
        tanh=False,
        mode="egnn_dynamics",
        norm_constant=0,
        inv_sublayers=2,
        sin_embedding=False,
        sin_encoding=False,
        sin_encoding_freq=1 / 9,
        normalization_factor=100,
        aggregation_method="sum",
        update_pocket_coords=True,
        edge_cutoff=None,
        use_nodes_noise_prediction=True,
    ):
        super().__init__()
        self.mode = mode
        self.edge_cutoff = edge_cutoff
        self.use_nodes_noise_prediction = use_nodes_noise_prediction

        self.atom_encoder = nn.Sequential(
            nn.Linear(atom_nf, 2 * atom_nf), act_fn, nn.Linear(2 * atom_nf, joint_nf)
        )

        # self.atom_decoder = nn.Sequential(
        #     nn.Linear(joint_nf, 2 * atom_nf),
        #     act_fn,
        #     nn.Linear(2 * atom_nf, atom_nf)
        # )
        self.atom_decoder = nn.Sequential(
            nn.Linear(joint_nf, 2 * atom_nf),
            act_fn,
            nn.Linear(2 * atom_nf, atom_nf),
            act_fn,
            nn.Linear(atom_nf, n_dims),
        )

        self.residue_encoder = nn.Sequential(
            nn.Linear(residue_nf, 2 * residue_nf),
            act_fn,
            nn.Linear(2 * residue_nf, joint_nf),
        )

        self.residue_decoder = nn.Sequential(
            nn.Linear(joint_nf, 2 * residue_nf),
            act_fn,
            nn.Linear(2 * residue_nf, residue_nf),
        )

        if condition_time:
            dynamics_node_nf = joint_nf + 1
        else:
            print("Warning: dynamics model is _not_ conditioned on time.")
            dynamics_node_nf = joint_nf

        if mode == "egnn_dynamics":
            self.egnn = EGNN(
                in_node_nf=dynamics_node_nf,
                in_edge_nf=joint_nf,
                hidden_nf=hidden_nf,
                device=device,
                act_fn=act_fn,
                n_layers=n_layers,
                attention=attention,
                tanh=tanh,
                norm_constant=norm_constant,
                inv_sublayers=inv_sublayers,
                sin_embedding=sin_embedding,
                sin_encoding=sin_encoding,
                normalization_factor=normalization_factor,
                aggregation_method=aggregation_method,
            )
            self.node_nf = dynamics_node_nf
            self.update_pocket_coords = update_pocket_coords

        elif mode == "gnn_dynamics":
            self.gnn = GNN(
                in_node_nf=dynamics_node_nf + n_dims,
                in_edge_nf=0,
                hidden_nf=hidden_nf,
                out_node_nf=n_dims + dynamics_node_nf,
                device=device,
                act_fn=act_fn,
                n_layers=n_layers,
                attention=attention,
                normalization_factor=normalization_factor,
                aggregation_method=aggregation_method,
            )

        if sin_encoding:
            self.sin_encoding = PositionalEncoding(
                joint_nf, base_freq=sin_encoding_freq, granularity=1 / math.pi
            )

        self.device = device
        self.n_dims = n_dims
        self.condition_time = condition_time

    def forward(self, xh_atoms, xh_residues, t, mask_atoms, mask_residues):

        x_atoms = xh_atoms[:, : self.n_dims].clone()
        h_atoms = xh_atoms[:, self.n_dims :].clone()

        x_residues = xh_residues[:, : self.n_dims].clone()
        h_residues = xh_residues[:, self.n_dims :].clone()

        # embed atom features and residue features in a shared space
        h_atoms = self.atom_encoder(h_atoms)
        h_residues = self.residue_encoder(h_residues)

        if self.sin_encoding is not None:
            _, sizes = torch.unique(mask_atoms, return_counts=True)
            h_atoms = h_atoms + self.sin_encoding(
                torch.concatenate([torch.arange(s) for s in sizes]).to(self.device)
            )

        # combine the two node types
        x = torch.cat((x_atoms, x_residues), dim=0)
        h = torch.cat((h_atoms, h_residues), dim=0)
        mask = torch.cat([mask_atoms, mask_residues])

        if self.condition_time:
            if np.prod(t.size()) == 1:
                # t is the same for all elements in batch.
                h_time = torch.empty_like(h[:, 0:1]).fill_(t.item())
            else:
                # t is different over the batch dimension.
                h_time = t[mask]
            h = torch.cat([h, h_time], dim=1)

        # get edges of a complete graph
        if self.edge_cutoff is None:
            edges=self.get_edges(mask, x)
        else:
            edges = self.get_edges_mhc_cutoff(
                mask_atoms, mask_residues, x_atoms, x_residues
            )

        if self.sin_encoding is not None:
            edge_attr = torch.zeros(edges.shape[1], h_atoms.shape[1]).to(self.device)
            # get the subset of edges between atoms and atoms for each batch
            edges_atoms = self.get_edges(mask_atoms, x_atoms)
            edge_diff = edges_atoms[0] - edges_atoms[1]
            atom_edge_attr = self.sin_encoding(edge_diff)

            _, sizes = torch.unique(edges_atoms[0], return_counts=True)
            atom_nodes = edges_atoms[0].unique()
            atom_starts = torch.searchsorted(edges[0], atom_nodes)
            atom_index = torch.repeat_interleave(atom_starts, sizes)

            atom_index = atom_index + torch.concatenate(
                [torch.arange(s).to(self.device) for s in sizes]
            )
            edge_attr[atom_index] = atom_edge_attr

        if self.mode == "egnn_dynamics":
            update_coords_mask = (
                None
                if self.update_pocket_coords
                else torch.cat(
                    (torch.ones_like(mask_atoms), torch.zeros_like(mask_residues))
                ).unsqueeze(1)
            )
            h_final, x_final = self.egnn(
                h, x, edges, update_coords_mask=update_coords_mask, edge_attr=edge_attr
            )
            vel = x_final - x

        elif self.mode == "gnn_dynamics":
            xh = torch.cat([x, h], dim=1)
            output = self.gnn(xh, edges, node_mask=None)
            vel = output[:, :3]
            h_final = output[:, 3:]

        else:
            raise Exception("Wrong mode %s" % self.mode)

        if self.condition_time:
            # Slice off last dimension which represented time.
            h_final = h_final[:, :-1]

        # decode atom and residue features
        h_final_atoms = self.atom_decoder(h_final[: len(mask_atoms)])
        h_final_residues = self.residue_decoder(h_final[len(mask_atoms) :])

        if torch.any(torch.isnan(vel)):
            print("Warning: detected nan, resetting EGNN output to zero.")
            vel = torch.zeros_like(vel)

        if self.update_pocket_coords:
            # in case of unconditional joint distribution, include this as in
            # the original code
            vel = remove_mean_batch(vel, mask)

        # return torch.cat([vel[:len(mask_atoms)], h_final_atoms], dim=-1), \
        #        torch.cat([vel[len(mask_atoms):], h_final_residues], dim=-1)
        noise_prediction = vel[: len(mask_atoms)]
        if self.use_nodes_noise_prediction:
            noise_prediction += h_final_atoms

        return noise_prediction

    def get_edges(self, batch_mask, x):
        # TODO: cache batches for each example in self._edges_dict[n_nodes]
        adj = batch_mask[:, None] == batch_mask[None, :]
        # if self.edge_cutoff is not None:
        #     adj = adj & (torch.cdist(x, x) <= self.edge_cutoff)
        edges = torch.stack(torch.where(adj), dim=0)
        return edges

    def get_edges_mhc_cutoff(
        self, batch_mask_peptide, batch_mask_mhc, x_peptide, x_mhc
    ):
        """
        Does the same as self.get_edges(), where a fully connected graph is created
        using batch_mask = [batch_mask_peptide, batch_mask_mhc] and x = [x_peptide, x_mhc],
        but instead cuts-off the edges within MHC only when they do not fall
        within the cutoff radius.
        """
        mask = torch.cat([batch_mask_peptide, batch_mask_mhc])
        x = torch.cat([x_peptide, x_mhc], dim=0)
        adj = mask[:, None] == mask[None, :]
        adj_mhc = torch.cdist(x_mhc, x_mhc) <= self.edge_cutoff
        adj[len(batch_mask_peptide) :, len(batch_mask_peptide) :] &= adj_mhc
        edges = torch.stack(torch.where(adj), dim=0)
        return edges
