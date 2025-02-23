import torch
from topomodelx import MessagePassing, Aggregation
from torch.nn import Sequential, Linear, Dropout
from torch_scatter import scatter_min, scatter_add, scatter_mean

from proteinworkshop.models.utils import get_activations
from topotein.models.aggregation import InterNeighborhoodAggregator, IntraNeighborhoodAggregator


def cast_dense_by_sparse_link(
        x: torch.Tensor,
        S1: torch.sparse_coo_tensor,
        S2: torch.sparse_coo_tensor
) -> torch.Tensor:
    """
    Cast a dense tensor x of shape [B, nnz_S1] (e.g. nnz_S1 = 4358)
    into a dense tensor of shape [B, nnz_S2] (e.g. nnz_S2 = 57376),
    using S1 and S2 as linking sparse tensors.

    The linking is done via the following logic:
      - S1: shape (483, 5045), nnz = 4358.
             Its indices: [2, nnz_S1], where the first row corresponds to row indices,
             and the second row corresponds to column indices (keys in range 0–5044).
      - S2: shape (5045, 5045), nnz = 57376.
             Its indices: [2, nnz_S2], where the first row contains keys (in range 0–5044)
             that link to S1, and the second row can be any column indices.

    For each nonzero in S2, we use its first index as a key. Then we build a mapping
    from each key (0 to 5044) to the first occurrence index in S1 that has that key.
    Finally, for each nonzero in S2, we “gather” the corresponding value from x along
    its second dimension.

    Args:
        x (torch.Tensor): Dense tensor of shape [B, nnz_S1], where B is batch size.
        S1 (torch.sparse_coo_tensor): Sparse tensor of shape (483, 5045) with nnz_S1 entries.
        S2 (torch.sparse_coo_tensor): Sparse tensor of shape (5045, 5045) with nnz_S2 entries.

    Returns:
        torch.Tensor: A dense tensor of shape [B, nnz_S2], where for each nonzero in S2,
                      we have assigned the value from x corresponding to the matching key.
                      (If a key is not found in S1, a 0 is placed.)
    """
    # --- Extract linking keys ---
    # For S1, we use its second row of indices as keys (values in 0 .. S1.size(1)-1).
    S1 = S1.coalesce()
    S1_idx = S1.indices()            # shape: [2, nnz_S1]
    keys_S1 = S1_idx[1]              # shape: [nnz_S1]

    # For S2, we use its first row of indices as keys.
    S2 = S2.coalesce()
    S2_idx = S2.indices()            # shape: [2, nnz_S2]
    keys_S2 = S2_idx[0]              # shape: [nnz_S2]

    # --- Build mapping from keys to first occurrence index in S1 ---
    # Create a tensor of positions corresponding to each nonzero in S1.
    positions = torch.arange(keys_S1.size(0), device=x.device)  # shape: [nnz_S1]
    # Use scatter_min to compute, for each key (0 .. S1.size(1)-1), the minimum index (first occurrence).
    # The output will be of shape [S1.size(1)] (i.e. 5045 elements).
    mapping_out, argmin = scatter_min(positions, keys_S1, dim=0, dim_size=S1.size(1))
    # For keys that did not occur in S1, scatter_min sets argmin to -1.
    # We explicitly mark these positions in mapping_out as -1.
    mapping_out[argmin == -1] = -1  # mapping: tensor of shape [S1.size(1)]

    # --- Use the mapping to gather values from x ---
    # For each nonzero in S2 (each key in keys_S2), look up the corresponding S1 index.
    mapped_idx = mapping_out[keys_S2]  # shape: [nnz_S2]

    # Create the output dense tensor of shape [B, nnz_S2], initialized with zeros.
    B = x.size(-1)
    y = torch.zeros(mapped_idx.size(0), B, dtype=x.dtype, device=x.device)

    # Determine positions where a valid mapping exists (mapped index is not -1).
    valid = mapped_idx != -1  # Boolean tensor of shape [nnz_S2]
    if valid.any():
        # Gather values from x along dimension 1 using the mapped indices.
        # x has shape [B, nnz_S1] and we index along dimension 1.
        y[valid, :] = x[mapped_idx[valid], :]

    return y


def compute_sparse_messages(M: torch.Tensor, H: torch.Tensor) -> torch.Tensor:
    """
    Computes the sparse messages based on the input sparse tensor M and dense
    tensor H. This function utilizes the `coalesce` method to ensure that
    the input sparse tensor M has no duplicate indices and is in a canonical
    format. The function multiplies the values of the sparse tensor M with
    the corresponding rows in the dense tensor H indicated by the indices
    of M.

    :param M: Torch sparse tensor where the sparse indices and values
              represent the input data. Must be in a canonical format.
    :param H: Dense tensor where the rows are selected based on the indices
              of the sparse tensor M. This tensor is used to scale the sparse
              values of M.
    :return: A dense tensor where each row is generated by multiplying the
             values of sparse tensor M with the corresponding rows of
             the dense tensor H as per the indices of M.
    :rtype: torch.Tensor
    """
    M = M.coalesce()
    return M.values().unsqueeze(1) * H[M.indices()[1]]  # shape: [nnz, d_in]


def intra_neighborhood_agg(M: torch.Tensor, msgs: torch.Tensor) -> torch.Tensor:
    """
    Aggregates messages for nodes within the same neighborhood using the given
    coalesced sparse tensor representation.

    This function utilizes the `scatter_add` operation to sum the provided
    messages (`msgs`) for all nodes within the neighborhoods defined by the
    rows of the coalesced adjacency matrix `M`. The aggregation is performed
    along the first dimension of the `msgs` tensor, using the indices provided
    by `M.indices()[0]`. The result is a new tensor containing the aggregated
    messages for each node, with the size equal to the number of nodes in `M`.

    :param M: Coalesced sparse tensor representing the adjacency structure of
              the graph.
    :param msgs: Tensor of messages to be aggregated, where each message
                 corresponds to an edge in the graph.
    :return: Tensor containing the aggregated messages for each node in the
             graph, with shape `(number of nodes in M, ...)`.
    """
    M = M.coalesce()
    return scatter_mean(msgs, M.indices()[0], dim=0, dim_size=M.size(0))


class ETNNLayer(MessagePassing):
    def __init__(self, emb_dim: int, edge_attr_dim: int = 2, sse_attr_dim: int = 4, dropout: float = 0.1,
                 activation: str = "silu", norm: str = "batch", position_update=False, **kwargs) -> None:
        super(ETNNLayer, self).__init__()

        self.position_update = position_update
        if "layer_cfg" in kwargs:
            layer_cfg = kwargs.pop("layer_cfg")
            for k, v in layer_cfg.items():
                setattr(self, k, v)

        self.norm = {
            "layer": torch.nn.LayerNorm,
            "batch": torch.nn.BatchNorm1d,
        }[norm]
        self.activation = get_activations(activation)
        self.phi_sse = Sequential(
            Linear(2 * emb_dim + 1 + sse_attr_dim, emb_dim),
            self.norm(emb_dim),
            self.activation,
            Dropout(dropout),
            Linear(emb_dim, emb_dim),
            self.norm(emb_dim),
            self.activation,
            Dropout(dropout),
        )
        self.phi_edge = Sequential(
            Linear(2 * emb_dim + 1 + edge_attr_dim, emb_dim),
            self.norm(emb_dim),
            self.activation,
            Dropout(dropout),
            Linear(emb_dim, emb_dim),
            self.norm(emb_dim),
            self.activation,
            Dropout(dropout),
        )
        if self.position_update:
            self.phi_x = Sequential(
                Linear(emb_dim, emb_dim),
                self.norm(emb_dim),
                self.activation,
                Dropout(dropout),
                Linear(emb_dim, 1),
            )
        self.phi_update = Sequential(
            Linear(2 * emb_dim, emb_dim),
            self.norm(emb_dim),
            self.activation,
            Dropout(dropout),
            Linear(emb_dim, emb_dim),
            self.norm(emb_dim),
            self.activation,
            Dropout(dropout),
        )
        self.agg_intra = IntraNeighborhoodAggregator(aggr_func="sum")
        self.agg_inter = InterNeighborhoodAggregator(aggr_func="sum")


    def forward(self, X, H0, H1, H2, N0_0_via_1, N0_0_via_2, N2_0, N1_0):
        # step 1 - message passing
        msg_sse, msg_edge = self.message(X, H0, H1, H2, N0_0_via_1, N0_0_via_2, N2_0, N1_0)

        # step 2 - intra-neighborhood aggregation
        if self.position_update:
            msg_pos_via_1 = self.weighted_distance_difference(X, N0_0_via_1, N1_0, self.phi_x(msg_edge))
            msg_pos_via_2 = self.weighted_distance_difference(X, N0_0_via_2, N2_0, self.phi_x(msg_sse))

        msg_sse = self.agg_intra(N0_0_via_2, msg_sse)
        msg_edge = self.agg_intra(N0_0_via_1, msg_edge)
        # step 3 - inter-neighborhood aggregation
        h_update = self.agg_inter([msg_sse, msg_edge])

        if self.position_update:
            x_update = self.agg_inter([msg_pos_via_1, msg_pos_via_2])
        else:
            x_update = 0

        # step 4 - update
        H0_update = self.phi_update(torch.cat([H0, h_update], dim=-1))
        H0 = H0 + H0_update
        X = X + x_update
        return H0, X

    def message(self, X, H0, H1, H2, N0_0_via_1, N0_0_via_2, N2_0, N1_0):

        H0_i = H0[N0_0_via_2.indices()[0]]
        H0_j = H0[N0_0_via_2.indices()[1]]
        X_i = X[N0_0_via_2.indices()[0]]
        X_j = X[N0_0_via_2.indices()[1]]
        dist_norm = torch.norm(X_i - X_j, dim=-1).view(-1, 1)
        msg_sse = self.phi_sse(torch.cat([
            H0_i,
            H0_j,
            dist_norm,
            cast_dense_by_sparse_link(compute_sparse_messages(N2_0.T, H2), N2_0, N0_0_via_2)
        ], dim=-1))

        H0_i = H0[N0_0_via_1.indices()[0]]
        H0_j = H0[N0_0_via_1.indices()[1]]
        X_i = X[N0_0_via_1.indices()[0]]
        X_j = X[N0_0_via_1.indices()[1]]
        dist_norm = torch.norm(X_i - X_j, dim=-1).view(-1, 1)
        msg_edge = self.phi_edge(torch.cat([
            H0_i,
            H0_j,
            dist_norm,
            cast_dense_by_sparse_link(compute_sparse_messages(N1_0.T, H1), N1_0, N0_0_via_1)
        ], dim=-1))
        return msg_sse, msg_edge

    def weighted_distance_difference(self, X, A, B, weights):
        # Adjust weights according to the sparse/dense linkage.
        weights = cast_dense_by_sparse_link(weights, B, A)

        # 1. Get the edge indices.
        edge_indices = A._indices()  # Shape: [2, num_edges]
        source_indices = edge_indices[0]  # 1D tensor of source node indices
        target_indices = edge_indices[1]  # 1D tensor of target node indices

        # 2. Gather the coordinates.
        source_coords = X[source_indices]  # Shape: [num_edges, 3]
        target_coords = X[target_indices]  # Shape: [num_edges, 3]

        # 3. Compute differences and normalize each.
        diffs = source_coords - target_coords  # Shape: [num_edges, 3]
        norms = diffs.norm(dim=1, keepdim=True)  # Shape: [num_edges, 1]
        epsilon = 1
        normalized_diffs = weights * diffs / (norms + epsilon)  # Shape: [num_edges, 3]

        # 4. Aggregate the normalized differences by computing the mean for each source node.
        n = X.size(0)

        # Sum the normalized differences for each source node.
        result_sum = torch.zeros(n, 3, device=X.device)
        result_sum = result_sum.index_add(0, source_indices, normalized_diffs)

        # Count the number of contributions (edges) per source node.
        counts = torch.zeros(n, device=X.device)
        ones = torch.ones(source_indices.size(0), device=X.device)
        counts = counts.index_add(0, source_indices, ones)

        # Compute the mean by dividing the sum by the count (avoid division by zero).
        result_mean = result_sum / counts.clamp(min=1).unsqueeze(1)

        return result_mean

#%%
if __name__ == "__main__":
    #%%
    batch = torch.load("/Users/dricpro/PycharmProjects/Topotein/test/data/sample_batch/sample_featurised_batch_edge_processed_simple.pt", weights_only=False)
    print(batch)
    #%%
    from toponetx import CellComplex
    from topomodelx.utils.sparse import from_sparse

    X = batch.pos
    H0 = batch.x
    H1 = batch.edge_attr
    H2 = batch.sse_attr

    device = X.device

    cc: CellComplex = batch.sse_cell_complex
    Bt = [from_sparse(cc.incidence_matrix(rank=i, signed=False).T).to(device) for i in range(1,3)]
    N2_0 = (torch.sparse.mm(Bt[1], Bt[0]) / 2).coalesce()
    N1_0 = Bt[0].coalesce()
    N0_0_via_1 = from_sparse(cc.adjacency_matrix(rank=0, signed=False)).to(device)
    N0_0_via_2 = torch.sparse.mm(N2_0.T, N2_0).coalesce()

    #%%
    emb = torch.randn(57, 512)
    H0 = H0 @ emb
    layer = ETNNLayer(emb_dim=512, edge_attr_dim=2, sse_attr_dim=4, dropout=0, activation="silu", norm="batch")
    import time
    tik = time.time()
    H, pos = layer(X, H0, H1, H2, N0_0_via_1, N0_0_via_2, N2_0, N1_0)
    tok = time.time()
    print(f"Time taken: {tok-tik:.2f}s")
    Q = torch.randn(3, 3)
    t = torch.rand(3)
    posQt = pos @ Q + t

    QtH, QtPos = layer(X @ Q + t, H0, H1, H2, N0_0_via_1, N0_0_via_2, N2_0, N1_0)

    assert torch.allclose(H, QtH, atol=10), f"Hidden state is not invariant to Q and t\n{H}\n{QtH}"
    # assert torch.allclose(H, QtH, atol=1), f"Hidden state is not invariant to Q and t\n{H}\n{QtH}"
    # assert torch.allclose(H, QtH, atol=.1), f"Hidden state is not invariant to Q and t\n{H}\n{QtH}"

    assert torch.allclose(posQt, QtPos, atol=10), f"Position is not equivariant to Q and t\n{posQt}\n{QtPos}"
    assert torch.allclose(posQt, QtPos, atol=1), f"Position is not equivariant to Q and t\n{posQt}\n{QtPos}"
    assert torch.allclose(posQt, QtPos, atol=.1), f"Position is not equivariant to Q and t\n{posQt}\n{QtPos}"

    print("All tests passed")

    #%%
    print(pos)
    print(X)

    #%%
    (pos - X).abs().max()
    #%%
    (pos - X).abs().mean()
    #%%
    (pos - X).mean()
