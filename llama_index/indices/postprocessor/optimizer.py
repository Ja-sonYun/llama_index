"""Optimization related classes and functions."""
import logging
from typing import Callable, List, Optional

try:
    from pydantic.v1 import Field, PrivateAttr
except ImportError:
    from pydantic import Field, PrivateAttr

from llama_index.embeddings.base import BaseEmbedding
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.indices.postprocessor.types import BaseNodePostprocessor
from llama_index.indices.query.embedding_utils import get_top_k_embeddings
from llama_index.indices.query.schema import QueryBundle
from llama_index.schema import MetadataMode, NodeWithScore

logger = logging.getLogger(__name__)


class SentenceEmbeddingOptimizer(BaseNodePostprocessor):
    """Optimization of a text chunk given the query by shortening the input text."""

    percentile_cutoff: Optional[float] = Field(
        description="Percentile cutoff for the top k sentences to use."
    )
    threshold_cutoff: Optional[float] = Field(
        description="Threshold cutoff for similiarity for each sentence to use."
    )

    _embed_model: BaseEmbedding = PrivateAttr()
    _tokenizer_fn: Callable[[str], List[str]] = PrivateAttr()

    def __init__(
        self,
        embed_model: Optional[BaseEmbedding] = None,
        percentile_cutoff: Optional[float] = None,
        threshold_cutoff: Optional[float] = None,
        tokenizer_fn: Optional[Callable[[str], List[str]]] = None,
    ):
        """Optimizer class that is passed into BaseGPTIndexQuery.

        Should be set like this:

        .. code-block:: python
        from llama_index.optimization.optimizer import Optimizer
        optimizer = SentenceEmbeddingOptimizer(
                        percentile_cutoff=0.5
                        this means that the top 50% of sentences will be used.
                        Alternatively, you can set the cutoff using a threshold
                        on the similarity score. In this case only sentences with a
                        similarity score higher than the threshold will be used.
                        threshold_cutoff=0.7
                        these cutoffs can also be used together.
                    )

        query_engine = index.as_query_engine(
            optimizer=optimizer
        )
        response = query_engine.query("<query_str>")
        """
        self._embed_model = embed_model or OpenAIEmbedding()

        if tokenizer_fn is None:
            import nltk.data
            import os
            from llama_index.utils import get_cache_dir

            cache_dir = get_cache_dir()
            nltk_data_dir = os.environ.get("NLTK_DATA", cache_dir)

            # update nltk path for nltk so that it finds the data
            if nltk_data_dir not in nltk.data.path:
                nltk.data.path.append(nltk_data_dir)

            try:
                nltk.data.find("tokenizers/punkt")
            except LookupError:
                nltk.download("punkt", download_dir=nltk_data_dir)

            tokenizer = nltk.data.load("tokenizers/punkt/english.pickle")
            tokenizer_fn = tokenizer.tokenize
        self._tokenizer_fn = tokenizer_fn

        super().__init__(
            percentile_cutoff=percentile_cutoff,
            threshold_cutoff=threshold_cutoff,
        )

    @classmethod
    def class_name(cls) -> str:
        return "SentenceEmbeddingOptimizer"

    def postprocess_nodes(
        self,
        nodes: List[NodeWithScore],
        query_bundle: Optional[QueryBundle] = None,
    ) -> List[NodeWithScore]:
        """Optimize a node text given the query by shortening the node text."""
        if query_bundle is None:
            return nodes

        for node_idx in range(len(nodes)):
            text = nodes[node_idx].node.get_content(metadata_mode=MetadataMode.LLM)

            split_text = self._tokenizer_fn(text)

            if query_bundle.embedding is None:
                query_bundle.embedding = (
                    self._embed_model.get_agg_embedding_from_queries(
                        query_bundle.embedding_strs
                    )
                )

            text_embeddings = self._embed_model._get_text_embeddings(split_text)

            num_top_k = None
            threshold = None
            if self.percentile_cutoff is not None:
                num_top_k = int(len(split_text) * self.percentile_cutoff)
            if self.threshold_cutoff is not None:
                threshold = self.threshold_cutoff

            top_similarities, top_idxs = get_top_k_embeddings(
                query_embedding=query_bundle.embedding,
                embeddings=text_embeddings,
                similarity_fn=self._embed_model.similarity,
                similarity_top_k=num_top_k,
                embedding_ids=list(range(len(text_embeddings))),
                similarity_cutoff=threshold,
            )

            if len(top_idxs) == 0:
                raise ValueError("Optimizer returned zero sentences.")

            top_sentences = [split_text[idx] for idx in top_idxs]

            logger.debug(f"> Top {len(top_idxs)} sentences with scores:\n")
            if logger.isEnabledFor(logging.DEBUG):
                for idx in range(len(top_idxs)):
                    logger.debug(
                        f"{idx}. {top_sentences[idx]} ({top_similarities[idx]})"
                    )

            nodes[node_idx].node.set_content(" ".join(top_sentences))

        return nodes
