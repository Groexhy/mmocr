import torch
from mmcv.cnn import normal_init
from torch import nn
from torch.nn import functional as F

from mmdet.models.builder import HEADS, build_loss


@HEADS.register_module()
class SDMGRHead(nn.Module):

    def __init__(self,
                 num_chars=92,
                 visual_dim=64,
                 fusion_dim=1024,
                 node_input=32,
                 node_embed=256,
                 edge_input=5,
                 edge_embed=256,
                 num_gnn=2,
                 num_classes=26,
                 loss=dict(type='SDMGRLoss'),
                 bidirectional=False,
                 train_cfg=None,
                 test_cfg=None):
        super().__init__()

        self.fusion = Block([visual_dim, node_embed], node_embed, fusion_dim)
        self.node_embed = nn.Embedding(num_chars, node_input, 0)
        hidden = node_embed // 2 if bidirectional else node_embed
        self.rnn = nn.LSTM(
            input_size=node_input,
            hidden_size=hidden,
            num_layers=1,
            batch_first=True,
            bidirectional=bidirectional)
        self.edge_embed = nn.Linear(edge_input, edge_embed)
        self.gnn_layers = nn.ModuleList(
            [GNNLayer(node_embed, edge_embed) for _ in range(num_gnn)])
        self.node_cls = nn.Linear(node_embed, num_classes)
        self.edge_cls = nn.Linear(edge_embed, 2)
        self.loss = build_loss(loss)

    def init_weights(self, pretrained=False):
        normal_init(self.edge_embed, mean=0, std=0.01)

    def forward(self, relations, texts, index, auxiliary_matrix, x=None):
        node_nums, char_nums = [], []
        texts = texts.squeeze(dim=1)
        node_nums = [texts.data.size(1)] * texts.data.size(0)
        # for text in texts:
        #     node_nums.append(text.size(0))
        #     char_nums.append((text > 0).sum(-1))

        all_nums = torch.clamp(texts, 0, 1).sum(-1).flatten().long()
        # max_num = max([char_num.max() for char_num in char_nums])
        # all_nodes = torch.cat([
        #     torch.cat(
        #         [text,
        #          text.new_zeros(text.size(0), max_num - text.size(1))], -1)
        #     for text in texts
        # ])
        # embed_nodes = self.node_embed(all_nodes.clamp(min=0).long())

        embed_nodes = self.node_embed(texts)
        rnn_nodes = self.rnn(embed_nodes)

        # nodes = rnn_nodes.new_zeros(*rnn_nodes.shape[::2])
        # all_nums = torch.cat(char_nums)
        # valid = all_nums > 0
        # nodes[valid] = rnn_nodes[valid].gather(
        #     1, (all_nums[valid] - 1).unsqueeze(-1).unsqueeze(-1).expand(
        #         -1, -1, rnn_nodes.size(-1))).squeeze(1)

        rnn_nodes = rnn_nodes.reshape(rnn_nodes.data.size(0), -1, rnn_nodes.data.size(-1))
        nodes = torch.index_select(rnn_nodes, dim=1, index=index)

        if x is not None:
            nodes = self.fusion([x, nodes])

        # all_edges = torch.cat(
        #     [rel.view(-1, rel.size(-1)) for rel in relations])
        embed_edges = self.edge_embed(relations)
        # embed_edges = F.normalize(embed_edges)

        eps = 1e-12
        embed_edges = embed_edges * ((F.relu(embed_edges.pow(2).sum(-1, keepdim=True).pow(0.5) - eps) + eps).pow(-1))

        for gnn_layer in self.gnn_layers:
            nodes, cat_nodes = gnn_layer(nodes, embed_edges, auxiliary_matrix, node_nums)

        node_cls, edge_cls = self.node_cls(nodes), self.edge_cls(cat_nodes)
        return node_cls, edge_cls


class GNNLayer(nn.Module):

    def __init__(self, node_dim=256, edge_dim=256):
        super().__init__()
        self.in_fc = nn.Linear(node_dim * 2 + edge_dim, node_dim)
        self.coef_fc = nn.Linear(node_dim, 1)
        self.out_fc = nn.Linear(node_dim, node_dim)
        self.relu = nn.ReLU()

    def forward(self, nodes, edges, auxiliary_matrix, nums):
        start, cat_nodes = 0, []
        # for num in nums:
        #     sample_nodes = nodes[start:start + num]
        #     cat_nodes.append(
        #         torch.cat([
        #             sample_nodes.unsqueeze(1).expand(-1, num, -1),
        #             sample_nodes.unsqueeze(0).expand(num, -1, -1)
        #         ], -1).view(num**2, -1))
        #     start += num

        start_nodes = nodes.unsqueeze(2)
        end_nodes = nodes.unsqueeze(1)
        temp_ones = torch.ones(nodes.data.size(0), nodes.data.size(1), nodes.data.size(1), nodes.data.size(2))
        start_nodes = start_nodes * temp_ones
        end_nodes = end_nodes * temp_ones
        cat_nodes = torch.cat([start_nodes, end_nodes, edges], dim=-1)

        cat_nodes = self.relu(self.in_fc(cat_nodes))
        coefs = self.coef_fc(cat_nodes)

        start, residuals = 0, []

        # for num in nums:
        #     residual = F.softmax(
        #         -torch.eye(num).to(coefs.device).unsqueeze(-1) * 1e9 +
        #         coefs[start:start + num**2].view(num, num, -1), 1)
        #     residuals.append(
        #         (residual *
        #          cat_nodes[start:start + num**2].view(num, num, -1)).sum(1))
        #     start += num**2

        residuals = (F.softmax(auxiliary_matrix + coefs, dim=2) * cat_nodes).permute((0, 1, 3, 2)).sum(3, keepdim=False)
        nodes += self.relu(self.out_fc(residuals))
        return nodes, cat_nodes


class Block(nn.Module):

    def __init__(self,
                 input_dims,
                 output_dim,
                 mm_dim=1600,
                 chunks=20,
                 rank=15,
                 shared=False,
                 dropout_input=0.,
                 dropout_pre_lin=0.,
                 dropout_output=0.,
                 pos_norm='before_cat'):
        super().__init__()
        self.rank = rank
        self.dropout_input = dropout_input
        self.dropout_pre_lin = dropout_pre_lin
        self.dropout_output = dropout_output
        assert (pos_norm in ['before_cat', 'after_cat'])
        self.pos_norm = pos_norm
        # Modules
        self.linear0 = nn.Linear(input_dims[0], mm_dim)
        self.linear1 = (
            self.linear0 if shared else nn.Linear(input_dims[1], mm_dim))
        self.merge_linears0 = nn.ModuleList()
        self.merge_linears1 = nn.ModuleList()
        self.chunks = self.chunk_sizes(mm_dim, chunks)
        for size in self.chunks:
            ml0 = nn.Linear(size, size * rank)
            self.merge_linears0.append(ml0)
            ml1 = ml0 if shared else nn.Linear(size, size * rank)
            self.merge_linears1.append(ml1)
        self.linear_out = nn.Linear(mm_dim, output_dim)

    def forward(self, x):
        x0 = self.linear0(x[0])
        x1 = self.linear1(x[1])
        bs = x1.size(0)
        if self.dropout_input > 0:
            x0 = F.dropout(x0, p=self.dropout_input, training=self.training)
            x1 = F.dropout(x1, p=self.dropout_input, training=self.training)
        x0_chunks = torch.split(x0, self.chunks, -1)
        x1_chunks = torch.split(x1, self.chunks, -1)
        zs = []
        for x0_c, x1_c, m0, m1 in zip(x0_chunks, x1_chunks,
                                      self.merge_linears0,
                                      self.merge_linears1):
            m = m0(x0_c) * m1(x1_c)  # bs x split_size*rank
            m = m.view(bs, self.rank, -1)
            z = torch.sum(m, 1)
            if self.pos_norm == 'before_cat':
                z = torch.sqrt(F.relu(z)) - torch.sqrt(F.relu(-z))
                z = F.normalize(z)
            zs.append(z)
        z = torch.cat(zs, 1)
        if self.pos_norm == 'after_cat':
            z = torch.sqrt(F.relu(z)) - torch.sqrt(F.relu(-z))
            z = F.normalize(z)

        if self.dropout_pre_lin > 0:
            z = F.dropout(z, p=self.dropout_pre_lin, training=self.training)
        z = self.linear_out(z)
        if self.dropout_output > 0:
            z = F.dropout(z, p=self.dropout_output, training=self.training)
        return z

    @staticmethod
    def chunk_sizes(dim, chunks):
        split_size = (dim + chunks - 1) // chunks
        sizes_list = [split_size] * chunks
        sizes_list[-1] = sizes_list[-1] - (sum(sizes_list) - dim)
        return sizes_list
