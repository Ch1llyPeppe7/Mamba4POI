import torch
from torch import nn
from Modules.MetaMamba import Mamba
from recbole.model.abstract_recommender import SequentialRecommender
from recbole.model.loss import BPRLoss
from Modules.myutils import *

class Mamba4POI(SequentialRecommender):
    def __init__(self, config, dataset):
        super(Mamba4POI, self).__init__(config, dataset)
        self.hidden_size = config["hidden_size"]
        self.loss_type = config["loss_type"]
        self.num_layers = config["num_layers"]
        self.dropout_prob = config["dropout_prob"]
        
        # Hyperparameters for Mamba block
        self.d_state = config["d_state"]
        self.d_conv = config["d_conv"]
        self.expand = config["expand"]

        self.locdim=int(self.hidden_size/3)
        self.catdim=int(self.hidden_size/3)
        self.usrdim=int(self.hidden_size/3)

        self.item_embedding,self.user_embedding = self._init_embedding(dataset)
  
        self.Norm = nn.LayerNorm(self.locdim, eps=1e-12)  # 针对地理位置编码
   
        self.dropout = nn.Dropout(self.dropout_prob)
        
        self.mamba_layers = nn.ModuleList([
            MambaLayer(
                d_model=self.hidden_size,
                d_state=self.d_state,
                d_conv=self.d_conv,
                expand=self.expand,
                dropout=self.dropout_prob,
                num_layers=self.num_layers,
            ) for _ in range(self.num_layers)
        ])
        
        if self.loss_type == "BPR":
            self.loss_fct = BPRLoss()
        elif self.loss_type == "CE":
            self.loss_fct = nn.CrossEntropyLoss()
        else:
            raise NotImplementedError("Make sure 'loss_type' in ['BPR', 'CE']!")

    def _init_embedding(self,dataset):
        num_category=(dataset.item_feat["venue_category_id"].max()+1).to(torch.int)
        num_user=(dataset.inter_feat["user_id"].max()+1).to(torch.int)
        _,M0,_,_=counting4all(dataset,self.device)

        Ec=torch.zeros((num_category,self.catdim),dtype=torch.float32)
        Eu=torch.zeros((num_user,self.catdim),dtype=torch.float32)
        row_nonzero_mask=M0.sum(1)>0
        col_nonzero_mask=M0.sum(0)>0
        nonzero_M0=M0[row_nonzero_mask][:,col_nonzero_mask]
        
        E0u,S0,E0c=UC_SVD(nonzero_M0,self.catdim,self.device)
        Ec[col_nonzero_mask]=E0c
        Eu[row_nonzero_mask]=E0u

        itemX=dataset.item_feat["longitude"]
        itemY=dataset.item_feat["latitude"]
        itemC=dataset.item_feat["venue_category_id"]
        Locations=torch.stack([itemX,itemY],dim=1)

        LocEnco=sinusoidal_position_encoding(Locations,self.locdim,self.device)

        embedding_prior=torch.concat((LocEnco,Ec[itemC]),dim=1)

        item_embedding_layer = nn.Embedding.from_pretrained(
            embedding_prior, freeze=False
        )

        user_embedding_layer = nn.Embedding.from_pretrained(
            Eu, freeze=False
        )
        return item_embedding_layer,user_embedding_layer


    def _init_weights(self, module):
        if isinstance(module, (nn.Linear, nn.Embedding)):
            module.weight.data.normal_(mean=0.0, std=0.02)
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()

    def forward(self, item_seq, item_seq_len,user_id):
        user_emb=self.user_embedding(user_id).unsqueeze(1).expand(item_seq.shape[0],item_seq.shape[1],-1)
        item_emb = self.item_embedding(item_seq)
        item_emb = self.dropout(item_emb)
        state_emb = torch.concat((self.Norm(user_emb),self.Norm(item_emb[:,:,:self.locdim]),
                        self.Norm(item_emb[:,:,self.locdim:])),dim=2)
        for i in range(self.num_layers):
            state_emb = self.mamba_layers[i](state_emb)
        
        seq_output = self.gather_indexes(state_emb, item_seq_len - 1)
        return seq_output

    def calculate_loss(self, interaction):
        user_id=interaction[self.USER_ID]
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]

        seq_output = self.forward(item_seq, item_seq_len,user_id)[:,self.usrdim:]
        pos_items = interaction[self.POS_ITEM_ID]
        
        if self.loss_type == "BPR":
            neg_items = interaction[self.NEG_ITEM_ID]
            pos_items_emb = self.item_embedding(pos_items)
            neg_items_emb = self.item_embedding(neg_items)
            pos_score = torch.sum(seq_output * pos_items_emb, dim=-1)  # [B]
            neg_score = torch.sum(seq_output * neg_items_emb, dim=-1)  # [B]
            loss = self.loss_fct(pos_score, neg_score)
            return loss
        else:  # self.loss_type = 'CE'
            test_item_emb = self.item_embedding.weight
            logits = torch.matmul(seq_output, test_item_emb.transpose(0, 1))
            loss = self.loss_fct(logits, pos_items)
        torch.cuda.empty_cache()    
        return loss

       


    def predict(self, interaction):
        user_id=interaction[self.USER_ID]
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        test_item = interaction[self.ITEM_ID]
        seq_output = self.forward(item_seq, item_seq_len,user_id)[:,self.usrdim:]
        test_item_emb = self.item_embedding(test_item)
        scores = torch.mul(seq_output, test_item_emb).sum(dim=1)  # [B]
        return scores

    def full_sort_predict(self, interaction):
        user_id=interaction[self.USER_ID]
        item_seq = interaction[self.ITEM_SEQ]
        item_seq_len = interaction[self.ITEM_SEQ_LEN]
        seq_output = self.forward(item_seq, item_seq_len,user_id)[:,self.usrdim:]
        test_items_emb = self.item_embedding.weight
        scores = torch.matmul(
            seq_output, test_items_emb.transpose(0, 1)
        )  # [B, n_items]
        return scores
    
class MambaLayer(nn.Module):
    def __init__(self, d_model, d_state, d_conv, expand, dropout, num_layers):
        super().__init__()
        self.num_layers = num_layers
        self.mamba = Mamba(
                # This module uses roughly 3 * expand * d_model^2 parameters
                d_model=d_model,
                d_state=d_state,
                d_conv=d_conv,
                expand=expand,
            )
        self.dropout = nn.Dropout(dropout)
        self.LayerNorm = nn.LayerNorm(d_model, eps=1e-12)
        self.ffn = FeedForward(d_model=d_model, inner_size=d_model*4, dropout=dropout)
    
    def forward(self, input_tensor):
        # 检查 input_tensor 维度，并添加 batch 维度
        if input_tensor.dim() == 2:  # 例如 [seq_len, d_model]
            input_tensor = input_tensor.unsqueeze(0)  # 添加 batch 维度 [1, seq_len, d_model]
        elif input_tensor.dim() == 1:  # 单个序列
            input_tensor = input_tensor.unsqueeze(0).unsqueeze(0)  # 变为 [1, 1, d_model]

        hidden_states = self.mamba(input_tensor)
        if self.num_layers == 1:
            hidden_states = self.LayerNorm(self.dropout(hidden_states))
        else:
            hidden_states = self.LayerNorm(self.dropout(hidden_states) + input_tensor)

        hidden_states = self.ffn(hidden_states)
        return hidden_states


class FeedForward(nn.Module):
    def __init__(self, d_model, inner_size, dropout=0.2):
        super().__init__()
        self.w_1 = nn.Linear(d_model, inner_size)
        self.w_2 = nn.Linear(inner_size, d_model)
        self.activation = nn.GELU()
        self.dropout = nn.Dropout(dropout)
        self.LayerNorm = nn.LayerNorm(d_model, eps=1e-12)

    def forward(self, input_tensor):
        hidden_states = self.w_1(input_tensor)
        hidden_states = self.activation(hidden_states)
        hidden_states = self.dropout(hidden_states)

        hidden_states = self.w_2(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)

        return hidden_states
    
    