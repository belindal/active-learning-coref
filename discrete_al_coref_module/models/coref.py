import logging
import math
from typing import Any, Dict, List, Optional, Tuple, Union
from retrying import retry

import torch
import torch.nn.functional as F
from overrides import overrides

from allennlp.data import Vocabulary
from allennlp.models.model import Model
from allennlp.modules.token_embedders import Embedding
from allennlp.modules import FeedForward
from allennlp.modules import Seq2SeqEncoder, TimeDistributed, TextFieldEmbedder
from allennlp.modules.span_extractors import SelfAttentiveSpanExtractor, EndpointSpanExtractor
from allennlp.nn import util, InitializerApplicator, RegularizerApplicator
from allennlp.training.metrics import MentionRecall, ConllCorefScores
from allennlp.models.coreference_resolution.coref import CoreferenceResolver

from discrete_al_coref_module.training import active_learning_coref_utils as al_util
from discrete_al_coref_module.models.pruner import Pruner

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


@Model.register("al_coref")
class ALCoreferenceResolver(CoreferenceResolver):
    """
    This ``Model`` implements the coreference resolution model described "End-to-end Neural
    Coreference Resolution"
    <https://www.semanticscholar.org/paper/End-to-end-Neural-Coreference-Resolution-Lee-He/3f2114893dc44eacac951f148fbff142ca200e83>
    by Lee et al., 2017.
    The basic outline of this model is to get an embedded representation of each span in the
    document. These span representations are scored and used to prune away spans that are unlikely
    to occur in a coreference cluster. For the remaining spans, the model decides which antecedent
    span (if any) they are coreferent with. The resulting coreference links, after applying
    transitivity, imply a clustering of the spans in the document.

    Parameters
    ----------
    vocab : ``Vocabulary``
    text_field_embedder : ``TextFieldEmbedder``
        Used to embed the ``text`` ``TextField`` we get as input to the model.
    context_layer : ``Seq2SeqEncoder``
        This layer incorporates contextual information for each word in the document.
    mention_feedforward : ``FeedForward``
        This feedforward network is applied to the span representations which is then scored
        by a linear layer.
    antecedent_feedforward: ``FeedForward``
        This feedforward network is applied to pairs of span representation, along with any
        pairwise features, which is then scored by a linear layer.
    feature_size: ``int``
        The embedding size for all the embedded features, such as distances or span widths.
    max_span_width: ``int``
        The maximum width of candidate spans.
    spans_per_word: float, required.
        A multiplier between zero and one which controls what percentage of candidate mention
        spans we retain with respect to the number of words in the document.
    max_antecedents: int, required.
        For each mention which survives the pruning stage, we consider this many antecedents.
    lexical_dropout: ``int``
        The probability of dropping out dimensions of the embedded text.
    initializer : ``InitializerApplicator``, optional (default=``InitializerApplicator()``)
        Used to initialize the model parameters.
    regularizer : ``RegularizerApplicator``, optional (default=``None``)
        If provided, will be used to calculate the regularization penalty during training.
    """
    def __init__(self,
                 vocab: Vocabulary,
                 text_field_embedder: TextFieldEmbedder,
                 context_layer: Seq2SeqEncoder,
                 mention_feedforward: FeedForward,
                 antecedent_feedforward: FeedForward,
                 feature_size: int,
                 max_span_width: int,
                 spans_per_word: float,
                 max_antecedents: int,
                 lexical_dropout: float = 0.2,
                 initializer: InitializerApplicator = InitializerApplicator(),
                 regularizer: Optional[RegularizerApplicator] = None) -> None:
        super(ALCoreferenceResolver, self).__init__(
            vocab,
            text_field_embedder,
            context_layer,
            mention_feedforward,
            antecedent_feedforward,
            feature_size,
            max_span_width,
            spans_per_word,
            max_antecedents,
            lexical_dropout,
            initializer,
            regularizer,
        )
        feedforward_scorer = torch.nn.Sequential(
                TimeDistributed(mention_feedforward),
                TimeDistributed(torch.nn.Linear(mention_feedforward.get_output_dim(), 1)))
        self._mention_pruner = Pruner(feedforward_scorer)

    @overrides
    def forward(self,  # type: ignore
                text: Dict[str, torch.LongTensor],
                spans: torch.IntTensor,
                span_labels: torch.IntTensor = None,
                metadata: List[Dict[str, Any]] = None,
                get_scores: bool = False,
                top_spans_info: Dict[str, torch.IntTensor] = None,
                coref_scores_info: Dict[str, torch.IntTensor] = None,
                return_mention_scores: bool = False,
                return_coref_scores: bool = False,
                **kwargs) -> Dict[str, torch.Tensor]:
        # pylint: disable=arguments-differ
        """
        Parameters
        ----------
        text : ``Dict[str, torch.LongTensor]``, required.
            The output of a ``TextField`` representing the text of
            the document.
        spans : ``torch.IntTensor``, required.
            A tensor of shape (batch_size, num_spans, 2), representing the inclusive start and end
            indices of candidate spans for mentions. Comes from a ``ListField[SpanField]`` of
            indices into the text of the document.
        span_labels : ``torch.IntTensor``, optional (default = None).
            A tensor of shape (batch_size, num_spans), representing the cluster ids
            of each span, or -1 for those which do not appear in any clusters.
        metadata : ``List[Dict[str, Any]]``, optional (default = None).
            A metadata dictionary for each instance in the batch. We use the "original_text" and "clusters" keys
            from this dictionary, which respectively have the original text and the annotated gold coreference
            clusters for that instance.

        Returns
        -------
        An output dictionary consisting of:
        top_spans : ``torch.IntTensor``
            A tensor of shape ``(batch_size, num_spans_to_keep, 2)`` representing
            the start and end word indices of the top spans that survived the pruning stage.
        antecedent_indices : ``torch.IntTensor``
            A tensor of shape ``(num_spans_to_keep, max_antecedents)`` representing for each top span
            the index (with respect to top_spans) of the possible antecedents the model considered.
        predicted_antecedents : ``torch.IntTensor``
            A tensor of shape ``(batch_size, num_spans_to_keep)`` representing, for each top span, the
            index (with respect to antecedent_indices) of the most likely antecedent. -1 means there
            was no predicted link.
        loss : ``torch.FloatTensor``, optional
            A scalar loss to be optimised.
        """
        if not coref_scores_info:
            if not top_spans_info:
                # Shape: (batch_size, document_length, embedding_size)
                text_embeddings = self._lexical_dropout(self._text_field_embedder(text))

                document_length = text_embeddings.size(1)
                num_spans = spans.size(1)

                # Shape: (batch_size, document_length)
                text_mask = util.get_text_field_mask(text).float()

                # Shape: (batch_size, num_spans)
                span_mask = (spans[:, :, 0] >= 0).squeeze(-1).float()
                # SpanFields return -1 when they are used as padding. As we do
                # some comparisons based on span widths when we attend over the
                # span representations that we generate from these indices, we
                # need them to be <= 0. This is only relevant in edge cases where
                # the number of spans we consider after the pruning stage is >= the
                # total number of spans, because in this case, it is possible we might
                # consider a masked span.
                # Shape: (batch_size, num_spans, 2)
                spans = F.relu(spans.float()).long()

                # Shape: (batch_size, document_length, encoding_dim)
                contextualized_embeddings = self._context_layer(text_embeddings, text_mask)
                # Shape: (batch_size, num_spans, 2 * encoding_dim + feature_size)
                endpoint_span_embeddings = self._endpoint_span_extractor(contextualized_embeddings, spans)
                # Shape: (batch_size, num_spans, emebedding_size)
                attended_span_embeddings = self._attentive_span_extractor(text_embeddings, spans)

                # Shape: (batch_size, num_spans, emebedding_size + 2 * encoding_dim + feature_size)
                span_embeddings = torch.cat([endpoint_span_embeddings, attended_span_embeddings], -1)

                # Prune based on mention scores.
                num_spans_to_keep = int(math.floor(self._spans_per_word * document_length))

                # get mention scores
                span_mention_scores = \
                    self._mention_pruner(span_embeddings, span_mask, spans.size(1), True)

                if return_mention_scores:
                    output_dict = {'num_spans_to_keep': num_spans_to_keep, 'mention_scores': span_mention_scores,
                                   'mask': span_mask.unsqueeze(-1), 'embeds': span_embeddings, 'text_mask': text_mask}
                    return output_dict

                (top_span_embeddings, top_span_mask,
                 top_span_indices, top_span_mention_scores) = self._mention_pruner(span_embeddings,
                                                                                   span_mask,
                                                                                   num_spans_to_keep,
                                                                                   False,
                                                                                   span_mention_scores)

                top_span_mask = top_span_mask.unsqueeze(-1)
                # Shape: (batch_size * num_spans_to_keep)
                # torch.index_select only accepts 1D indices, but here
                # we need to select spans for each element in the batch.
                # This reformats the indices to take into account their
                # index into the batch. We precompute this here to make
                # the multiple calls to util.batched_index_select below more efficient.
                flat_top_span_indices = util.flatten_and_batch_shift_indices(top_span_indices, num_spans)
            else:
                # for ensemble, ensemble_coref already implicitly computes top spans
                span_mention_scores = top_spans_info['mention_scores']
                top_span_mention_scores = top_spans_info['top_scores']
                num_spans_to_keep = top_span_mention_scores.size(1)
                top_span_indices = top_spans_info['span_indices']
                flat_top_span_indices = top_spans_info['flat_top_indices']
                top_span_mask = top_spans_info['top_mask']
                span_embeddings = top_spans_info['span_embeddings']
                top_span_embeddings = util.batched_index_select(span_embeddings, top_span_indices, flat_top_span_indices)
                text_mask = top_spans_info['text_mask']

            # Compute final predictions for which spans to consider as mentions.
            # Shape: (batch_size, num_spans_to_keep, 2)
            top_spans = util.batched_index_select(spans,
                                                  top_span_indices,
                                                  flat_top_span_indices)

            # Compute indices for antecedent spans to consider.
            max_antecedents = min(self._max_antecedents, num_spans_to_keep)

            # Now that we have our variables in terms of num_spans_to_keep, we need to
            # compare span pairs to decide each span's antecedent. Each span can only
            # have prior spans as antecedents, and we only consider up to max_antecedents
            # prior spans. So the first thing we do is construct a matrix mapping a span's
            #  index to the indices of its allowed antecedents. Note that this is independent
            #  of the batch dimension - it's just a function of the span's position in
            # top_spans. The spans are in document order, so we can just use the relative
            # index of the spans to know which other spans are allowed antecedents.

            # Once we have this matrix, we reformat our variables again to get embeddings
            # for all valid antecedents for each span. This gives us variables with shapes
            #  like (batch_size, num_spans_to_keep, max_antecedents, embedding_size), which
            #  we can use to make coreference decisions between valid span pairs.

            # Shapes:
            # (num_spans_to_keep, max_antecedents),
            # (1, max_antecedents),
            # (1, num_spans_to_keep, max_antecedents)
            valid_antecedent_indices, valid_antecedent_offsets, valid_antecedent_log_mask = \
                self._generate_valid_antecedents(num_spans_to_keep, max_antecedents, util.get_device_of(text_mask))
            # Select tensors relating to the antecedent spans.
            # Shape: (batch_size, num_spans_to_keep, max_antecedents, embedding_size)
            candidate_antecedent_embeddings = util.flattened_index_select(top_span_embeddings,
                                                                            valid_antecedent_indices)
            # Shape: (batch_size, num_spans_to_keep, max_antecedents)
            candidate_antecedent_mention_scores = util.flattened_index_select(top_span_mention_scores,
                                                                                valid_antecedent_indices).squeeze(-1)

            # Compute antecedent scores.
            # Shape: (batch_size, num_spans_to_keep, max_antecedents, embedding_size)
            span_pair_embeddings = self._compute_span_pair_embeddings(top_span_embeddings,
                                                                      candidate_antecedent_embeddings,
                                                                      valid_antecedent_offsets)
            # Shape: (batch_size, num_spans_to_keep, 1 + max_antecedents)
            coreference_scores = self._compute_coreference_scores(span_pair_embeddings,
                                                                  top_span_mention_scores,
                                                                  candidate_antecedent_mention_scores,
                                                                  valid_antecedent_log_mask)

            # We now have, for each span which survived the pruning stage,
            # a predicted antecedent. This implies a clustering if we group
            # mentions which refer to each other in a chain.
            # Shape: (batch_size, num_spans_to_keep)
            _, predicted_antecedents = coreference_scores.max(2)
            # Subtract one here because index 0 is the "no antecedent" class,
            # so this makes the indices line up with actual spans if the prediction
            # is greater than -1.
            predicted_antecedents -= 1

            output_dict = {"top_spans": top_spans,
                           "antecedent_indices": valid_antecedent_indices,
                           "predicted_antecedents": predicted_antecedents}
            if get_scores or return_coref_scores:
                output_dict["coreference_scores"] = coreference_scores
            if return_coref_scores:
                ret_values = {'output_dict': output_dict, 'top_span_inds': [top_span_indices, flat_top_span_indices],
                              'top_span_mask': top_span_mask, 'ant_mask': valid_antecedent_log_mask}
                return ret_values
        else:
            top_span_indices = coref_scores_info['top_span_inds'][0]
            flat_top_span_indices = coref_scores_info['top_span_inds'][1]
            top_span_mask = coref_scores_info['top_span_mask']
            valid_antecedent_log_mask = coref_scores_info['valid_antecedent_log_mask']
            
            output_dict = coref_scores_info['output_dict']
            top_spans = output_dict['top_spans']
            valid_antecedent_indices = output_dict['antecedent_indices']
            num_spans_to_keep = top_spans.size(1)
            flat_valid_antecedent_indices = util.flatten_and_batch_shift_indices(valid_antecedent_indices,
                                                                                 num_spans_to_keep)
            coreference_scores = output_dict['coreference_scores']
            _, predicted_antecedents = coreference_scores.max(2)
            predicted_antecedents -= 1
            output_dict['predicted_antecedents'] = predicted_antecedents
        if get_scores:
            output_dict['top_span_indices'] = top_span_indices 

        # top_span_indices
        if span_labels is not None:
            # Find the gold labels for the spans which we kept.
            pruned_gold_labels = util.batched_index_select(span_labels.unsqueeze(-1),
                                                           top_span_indices,
                                                           flat_top_span_indices)

            antecedent_labels = util.flattened_index_select(pruned_gold_labels,
                                                            valid_antecedent_indices).squeeze(-1)
            antecedent_labels += valid_antecedent_log_mask.long()

            # Compute labels.
            # Shape: (batch_size, num_spans_to_keep, max_antecedents + 1)
            gold_antecedent_labels = self._compute_antecedent_gold_labels(pruned_gold_labels,
                                                                          antecedent_labels)
            # Now, compute the loss using the negative marginal log-likelihood.
            # This is equal to the log of the sum of the probabilities of all antecedent predictions
            # that would be consistent with the data, in the sense that we are minimising, for a
            # given span, the negative marginal log likelihood of all antecedents which are in the
            # same gold cluster as the span we are currently considering. Each span i predicts a
            # single antecedent j, but there might be several prior mentions k in the same
            # coreference cluster that would be valid antecedents. Our loss is the sum of the
            # probability assigned to all valid antecedents. This is a valid objective for
            # clustering as we don't mind which antecedent is predicted, so long as they are in
            #  the same coreference cluster.
            coreference_log_probs = util.masked_log_softmax(coreference_scores, top_span_mask)
            correct_antecedent_log_probs = coreference_log_probs + gold_antecedent_labels.log()
            negative_marginal_log_likelihood = -util.logsumexp(correct_antecedent_log_probs).sum()

            self._mention_recall(top_spans, metadata)
            self._conll_coref_scores(top_spans, valid_antecedent_indices, predicted_antecedents, metadata)

            output_dict["loss"] = negative_marginal_log_likelihood

        if metadata is not None:
            output_dict["document"] = [x["original_text"] for x in metadata]
        return output_dict
