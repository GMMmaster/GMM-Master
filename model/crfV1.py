import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import math


class CRF(nn.Module):
    """
    Conditional random field.
    """

    def __init__(self, num_tags, emb_dim, beam_size=5, neg_nums=500, device='cpu', batch_first=True) -> None:
        super().__init__()
        self.num_tags = num_tags
        self.batch_first = batch_first
        self.device = device
        self.beam_size = beam_size
        self.neg_nums = neg_nums
        self.W = nn.Linear(emb_dim, emb_dim)

    def transition(self, tag1, tag2, full_road_emb, A_list):
        """
        tag1, tag2: (batch_size,)
        """
        # (batch_size, emb_dim)
        emb1 = self.W(full_road_emb[tag1]).unsqueeze(1)
        # (batch_size, emb_dim, 1)
        emb2 = full_road_emb[tag2].unsqueeze(-1)
        # (batch_size, )
        r = F.relu(torch.bmm(emb1, emb2)).squeeze()
        energy = A_list[tag1, tag2] * r
        return energy.flatten()

    def transitions(self, full_road_emb, A_list):
        # (num_tags, num_tags)
        attention = self.W(full_road_emb) @ full_road_emb.T
        energy = A_list * F.relu(attention)
        return energy

    def forward(self, emissions, tags, full_road_emb, A_list, mask):
        """
        Compute the conditional log likelihood of a sequence of tags given emission scores.
        emissions: (batch_size, seq_length, num_tags)
        tags: (batch_size, seq_length)
        mask: (batch_size, seq_length)
        Returns: 
            The log likelihood.
        """
        batch_size = mask.size(0)
        if self.batch_first:
            emissions = emissions.transpose(0, 1)
            tags = tags.transpose(0, 1)
            mask = mask.transpose(0, 1)
        # shape: (batch_size,)
        numerator = self._compute_score(emissions, tags, full_road_emb, A_list, mask)
        # shape: (batch_size,)
        denominator = self._compute_normalizer(emissions, full_road_emb, A_list, mask, numerator)
        # shape: (batch_size,)
        llh = numerator - denominator
        return llh.sum() / mask.float().sum()

    def decode(self, emissions, full_road_emb, A_list, mask):
        """
        Find the most likely tag sequence using Viterbi algorithm.
        emissions: (batch_size, seq_length, num_tags)
        mask: (batch_size, seq_length)
        Returns:
            List of list containing the best tag sequence for each batch.
        """
        if self.batch_first:
            emissions = emissions.transpose(0, 1)
            mask = mask.transpose(0, 1)
        return self._viterbi_decode(emissions, full_road_emb, A_list, mask)

    def _compute_score(self, emissions, tags, full_road_emb, A_list, mask):
        """
        S(X,y)
        emissions: (seq_length, batch_size, num_tags)
        tags: (seq_length, batch_size)
        mask: (seq_length, batch_size)
        return: (batch_size, )
        """

        seq_length, batch_size = tags.shape
        mask = mask.float()

        # Start transition score and first emission
        # shape: (batch_size,)
        score = torch.zeros(batch_size).to(self.device)
        score += emissions[0, torch.arange(batch_size), tags[0]]

        for i in range(1, seq_length):
            # Transition score to next tag, only added if next timestep is valid (mask == 1)
            # shape: (batch_size,)
            score += self.transition(tags[i - 1], tags[i], full_road_emb, A_list) * mask[i]

            # Emission score for next tag, only added if next timestep is valid (mask == 1)
            # shape: (batch_size,)
            score += emissions[i, torch.arange(batch_size), tags[i]] * mask[i]

        return score

    def _compute_normalizer(self, emissions, full_road_emb, A_list, mask, numerator):
        """
        emissions: (seq_length, batch_size, num_tags)
        mask: (seq_length, batch_size)
        """
        seq_length, batch_size = emissions.size(0), emissions.size(1)
        neg_tag_sets = np.random.choice(self.num_tags, self.neg_nums, replace=False)
        # Start transition score and first emission; score has size of
        # (batch_size, num_tags) where for each batch, the j-th column stores
        # the score that the first timestep has tag j
        # shape: (batch_size, num_tags)
        score = emissions[0, :, neg_tag_sets]
        trans = self.transitions(full_road_emb, A_list)[neg_tag_sets, :]
        trans = trans[:, neg_tag_sets]
        for i in range(1, seq_length):
            # Broadcast score for every possible next tag
            # shape: (batch_size, num_tags, 1)
            broadcast_score = score.unsqueeze(2)

            # Broadcast emission score for every possible current tag
            # shape: (batch_size, 1, num_tags)
            broadcast_emissions = emissions[i, :, neg_tag_sets].unsqueeze(1)

            # Compute the score tensor of size (batch_size, num_tags, num_tags) where
            # for each sample, entry at row i and column j stores the sum of scores of all
            # possible tag sequences so far that end with transitioning from tag i to tag j
            # and emitting
            # shape: (batch_size, num_tags, num_tags)
            next_score = broadcast_score + trans + broadcast_emissions
            # Sum over all possible current tags, but we're in score space, so a sum
            # becomes a log-sum-exp: for each sample, entry i stores the sum of scores of
            # all possible tag sequences so far, that end in tag i
            # shape: (batch_size, num_tags)
            next_score = torch.logsumexp(next_score, dim=1)

            # Set score to the next score if this timestep is valid (mask == 1)
            # shape: (batch_size, num_tags)
            score = torch.where(mask[i].unsqueeze(1), next_score, score)
        score = torch.cat((score, numerator.reshape(-1, 1)), dim=1)
        # Sum (log-sum-exp) over all possible tags
        # shape: (batch_size,)
        return torch.logsumexp(score, dim=1) + math.log(self.num_tags / (self.neg_nums + 1))

    def _viterbi_decode(self, emissions, full_road_emb, A_list, mask):
        """
        emissions: (seq_length, batch_size, num_tags)
        mask: (seq_length, batch_size)
        """

        seq_length, batch_size = mask.shape

        # Start transition and first emission
        # shape: (batch_size, num_tags)
        score = emissions[0]
        history = []

        # score is a tensor of size (batch_size, num_tags) where for every batch,
        # value at column j stores the score of the best tag sequence so far that ends
        # with tag j
        # history saves where the best tags candidate transitioned from; this is used
        # when we trace back the best tag sequence

        # Viterbi algorithm recursive case: we compute the score of the best tag sequence
        # for every possible next tag
        trans = self.transitions(full_road_emb, A_list)
        next_score = torch.zeros(batch_size, self.num_tags).to(self.device)
        indices = torch.zeros(batch_size, self.num_tags).int()
        for i in range(1, seq_length):
            # Broadcast viterbi score for every possible next tag
            # shape: (batch_size, num_tags, 1)
            # broadcast_score = score.unsqueeze(2)

            # Broadcast emission score for every possible current tag
            # shape: (batch_size, 1, num_tags)
            # broadcast_emission = emissions[i].unsqueeze(1)

            # Compute the score tensor of size (batch_size, num_tags, num_tags) where
            # for each sample, entry at row i and column j stores the score of the best
            # tag sequence so far that ends with transitioning from tag i to tag j and emitting
            # shape: (batch_size, num_tags, num_tags)
            # next_score = broadcast_score + trans + broadcast_emission
            for j in range(batch_size):
                cur_score, cur_indices = torch.max(score[j].unsqueeze(1) + trans + emissions[i,j,:].unsqueeze(0), dim=0)
                next_score[j] = cur_score
                indices[j] = cur_indices.cpu()
            # Find the maximum score over all possible current tag
            # shape: (batch_size, num_tags)
            # next_score, indices = next_score.max(dim=1)
            # print(next_score.shape)

            # Set score to the next score if this timestep is valid (mask == 1)
            # and save the index that produces the next score
            # shape: (batch_size, num_tags)
            score = torch.where(mask[i].unsqueeze(1), next_score, score)
            history.append(indices.clone())

        # Now, compute the best path for each sample
        # shape: (batch_size,)
        seq_ends = mask.long().sum(dim=0) - 1
        best_tags_list = []
        for idx in range(batch_size):
            # Find the tag which maximizes the score at the last timestep; this is our best tag
            # for the last timestep
            _, best_last_tag = score[idx].max(dim=0)
            best_tags = [best_last_tag.item()]

            # We trace back where the best last tag comes from, append that to our best tag
            # sequence, and trace it back again, and so on
            for hist in reversed(history[:seq_ends[idx]]):
                best_last_tag = hist[idx][best_tags[-1]]
                best_tags.append(best_last_tag.item())

            # Reverse the order because we start from the last timestep
            best_tags.reverse()
            tags_len = len(best_tags)
            best_tags_list.append(best_tags + [-1] * (seq_length - tags_len))
        return best_tags_list