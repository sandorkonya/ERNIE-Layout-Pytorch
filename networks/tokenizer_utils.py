# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import bisect
import itertools
import io
import json
import os
import six
import unicodedata
from collections import OrderedDict, UserDict
from shutil import copyfile
from typing import Iterable, Iterator, Optional, List, Any, Callable, Union
from typing import TYPE_CHECKING, Dict, NamedTuple, Sequence, Tuple
import re
import inspect

from .vocab import Vocab

from transformers.tokenization_utils_base import (AddedToken, BatchEncoding, EncodedInput,
                                                  EncodedInputPair, PreTokenizedInput,
                                                  PreTokenizedInputPair,
                                                  PreTrainedTokenizerBase, SpecialTokensMixin,
                                                  TextInput, TextInputPair, TruncationStrategy,
                                                  PaddingStrategy, TensorType)
from transformers.tokenization_utils import Trie

__all__ = [
    'PretrainedTokenizer', 'tokenize_chinese_chars',
    'is_chinese_char', 'normalize_chars', 'tokenize_special_chars',
    'convert_to_unicode'
]


def fn_args_to_dict(func, *args, **kwargs):
    """
    Inspect function `func` and its arguments for running, and extract a
    dict mapping between argument names and keys.
    """
    if hasattr(inspect, 'getfullargspec'):
        (spec_args, spec_varargs, spec_varkw, spec_defaults, _, _,
         _) = inspect.getfullargspec(func)
    else:
        (spec_args, spec_varargs, spec_varkw,
         spec_defaults) = inspect.getargspec(func)
    # add positional argument values
    init_dict = dict(zip(spec_args, args))
    # add default argument values
    kwargs_dict = dict(zip(spec_args[-len(spec_defaults):],
                           spec_defaults)) if spec_defaults else {}
    for k in list(kwargs_dict.keys()):
        if k in init_dict:
            kwargs_dict.pop(k)
    kwargs_dict.update(kwargs)
    init_dict.update(kwargs_dict)
    return init_dict


def convert_to_unicode(text):
    """
    Converts `text` to Unicode (if it's not already), assuming utf-8 input.
    Args:
        text (str|bytes): Text to be converted to unicode.
    Returns:
        str: converted text.
    """
    if isinstance(text, str):
        return text
    elif isinstance(text, bytes):
        return text.decode("utf-8", "ignore")
    else:
        raise ValueError("Unsupported string type: %s" % (type(text)))


def whitespace_tokenize(text):
    """
    Runs basic whitespace cleaning and splitting on a peice of text.
    Args:
        text (str): Text to be tokenized.
    Returns:
        list(str): Token list.
    """
    text = text.strip()
    if not text:
        return []
    tokens = text.split()
    return tokens


def _is_whitespace(char):
    """
    Checks whether `chars` is a whitespace character.
    """
    # \t, \n, and \r are technically contorl characters but we treat them
    # as whitespace since they are generally considered as such.
    if char == " " or char == "\t" or char == "\n" or char == "\r":
        return True
    cat = unicodedata.category(char)
    if cat == "Zs":
        return True
    return False


def _is_control(char):
    """Checks whether `chars` is a control character."""
    # These are technically control characters but we count them as whitespace
    # characters.
    if char == "\t" or char == "\n" or char == "\r":
        return False
    cat = unicodedata.category(char)
    if cat.startswith("C"):
        return True
    return False


def _is_punctuation(char):
    """Checks whether `chars` is a punctuation character."""
    cp = ord(char)
    # We treat all non-letter/number ASCII as punctuation.
    # Characters such as "^", "$", and "`" are not in the Unicode
    # Punctuation class but we treat them as punctuation anyways, for
    # consistency.
    if ((cp >= 33 and cp <= 47) or (cp >= 58 and cp <= 64)
            or (cp >= 91 and cp <= 96) or (cp >= 123 and cp <= 126)):
        return True
    cat = unicodedata.category(char)
    if cat.startswith("P"):
        return True
    return False


def _is_end_of_word(text):
    """Checks whether the last character in text is one of a punctuation, control or whitespace character."""
    last_char = text[-1]
    return bool(
        _is_control(last_char) | _is_punctuation(last_char)
        | _is_whitespace(last_char))


def _is_start_of_word(text):
    """Checks whether the first character in text is one of a punctuation, control or whitespace character."""
    first_char = text[0]
    return bool(
        _is_control(first_char) | _is_punctuation(first_char)
        | _is_whitespace(first_char))


def _insert_one_token_to_ordered_list(token_list: List[str], new_token: str):
    """
    Inserts one token to an ordered list if it does not already exist. Note: token_list must be sorted.
    """
    insertion_idx = bisect.bisect_left(token_list, new_token)
    # Checks if new_token is already in the ordered token_list
    if insertion_idx < len(
            token_list) and token_list[insertion_idx] == new_token:
        # new_token is in token_list, don't add
        return
    else:
        token_list.insert(insertion_idx, new_token)


def is_chinese_char(cp):
    """Checks whether CP is the codepoint of a CJK character."""
    # This defines a "chinese character" as anything in the CJK Unicode block:
    #     https://en.wikipedia.org/wiki/CJK_Unified_Ideographs_(Unicode_block)
    #
    # Note that the CJK Unicode block is NOT all Japanese and Korean characters,
    # despite its name. The modern Korean Hangul alphabet is a different block,
    # as is Japanese Hiragana and Katakana. Those alphabets are used to write
    # space-separated words, so they are not treated specially and handled
    # like the all of the other languages.
    if ((cp >= 0x4E00 and cp <= 0x9FFF) or  #
            (cp >= 0x3400 and cp <= 0x4DBF) or  #
            (cp >= 0x20000 and cp <= 0x2A6DF) or  #
            (cp >= 0x2A700 and cp <= 0x2B73F) or  #
            (cp >= 0x2B740 and cp <= 0x2B81F) or  #
            (cp >= 0x2B820 and cp <= 0x2CEAF) or
            (cp >= 0xF900 and cp <= 0xFAFF) or  #
            (cp >= 0x2F800 and cp <= 0x2FA1F)):  #
        return True

    return False


def _is_nonnormalized_char(char):
    """Check whther `chars` is a non-normalized character."""
    cp = ord(char)
    if ((0xFF00 <= cp <= 0xFFEF) or  # Halfwidth and Fullwidth Forms
            (0xFE50 <= cp <= 0xFE6B) or  # Small Form Variants
            (0x3358 <= cp <= 0x33FF) or  # CJK Compatibility
            (0x249C <= cp <= 0x24E9) or  # Enclosed Alphanumerics: Ⓛ ⒰
            (0x3200 <= cp <= 0x32FF)):  # Enclosed CJK Letters and Months
        return True

    return False


def _is_nonnormalized_numeric(char):
    """Check whether `chars` is a non-normalized numeric character."""
    cp = ord(char)
    if ((0x2460 <= cp <= 0x249B) or  #
            (0x24EA <= cp <= 0x24FF) or  #
            (0x2776 <= cp <= 0x2793) or  # Enclosed Alphanumerics
            (0x2160 <= cp <= 0x217F)):  # Number Forms
        return True

    return False


def normalize_chars(text):
    """
    Normalize the text for multiligual and chinese models. Unicode range:
    https://www.ling.upenn.edu/courses/Spring_2003/ling538/UnicodeRanges.html
    """
    output = []
    for char in text:
        if _is_nonnormalized_char(char):
            for c in unicodedata.normalize("NFKC", char):
                output.append(c)
        elif _is_nonnormalized_numeric(char):
            output.append(" ")
            for c in str(int(unicodedata.numeric(char))):
                output.append(c)
            output.append(" ")
        elif ord(char) == 0xF979:  # https://www.zhihu.com/question/20697984
            output.append("凉")
        else:
            output.append(char)
    return "".join(output)


def _is_symbol(char):
    """Check whether CP is the codepoint of a Symbol character."""
    cp = ord(char)
    if unicodedata.category(char).startswith('S') or (cp in [
        0x00ad, 0x00b2, 0x00ba, 0x3007, 0x00b5, 0x00d8, 0x014b, 0x01b1
    ]):
        return True
    return False


def tokenize_special_chars(text):
    """Adds whitespace around any special character."""
    output = []
    for char in text:
        cp = ord(char)
        if ((0x3040 <= cp <= 0x30FF) or  # Japanese
                (0x0370 <= cp <= 0x04FF) or  # Greek/Coptic & Cyrillic
                (0x0250 <= cp <= 0x02AF) or  # IPA
                _is_symbol(char)):
            output.append(" ")
            output.append(char)
            output.append(" ")
        else:
            output.append(char)
    return "".join(output)


def tokenize_chinese_chars(text):
    """Adds whitespace around any CJK character."""
    output = []
    buff = ""
    for char in text:
        cp = ord(char)
        if is_chinese_char(cp):
            if buff != "":
                output.append(buff)
                buff = ""
            output.append(char)
        else:
            buff += char

    if buff != "":
        output.append(buff)

    return output


class PretrainedTokenizer(PreTrainedTokenizerBase):
    """
    Base class for all tokenizers.

    Inherits from [`~tokenizer_utils_base.PretrainedTokenizerBase`].

    Handle all the shared methods for tokenization and special tokens as well as methods downloading/caching/loading
    pretrained tokenizers as well as adding tokens to the vocabulary.

    This class also contain the added tokens in a unified way on top of all tokenizers so we don't have to handle the
    specific vocabulary augmentation methods of the various underlying dictionary structures (BPE, sentencepiece...).

    - **resource_files_names** (`Dict[str, str]`) -- A dictionary with, as keys, the `__init__` keyword name of each
        vocabulary file required by the model, and as associated values, the filename for saving the associated file
        (string).
    - **pretrained_resource_files_map** (`Dict[str, Dict[str, str]]`) -- A dictionary of dictionaries, with the
        high-level keys being the `__init__` keyword name of each vocabulary file required by the model, the
        low-level being the `short-cut-names` of the pretrained models with, as associated values, the `url` to the
        associated pretrained vocabulary file.
    - **max_model_input_sizes** (`Dict[str, Optional[int]]`) -- A dictionary with, as keys, the `short-cut-names`
        of the pretrained models, and as associated values, the maximum length of the sequence inputs of this model,
        or `None` if the model has no maximum input size.
    - **pretrained_init_configuration** (`Dict[str, Dict[str, Any]]`) -- A dictionary with, as keys, the
        `short-cut-names` of the pretrained models, and as associated values, a dictionary of specific arguments to
        pass to the `__init__` method of the tokenizer class for this pretrained model when loading the tokenizer
        with the [`~tokenizer_utils_base.PretrainedTokenizerBase.from_pretrained`] method.
    - **model_input_names** (`List[str]`) -- A list of inputs expected in the forward pass of the model.
    - **padding_side** (`str`) -- The default value for the side on which the model should have padding applied.
        Should be `'right'` or `'left'`.
    - **truncation_side** (`str`) -- The default value for the side on which the model should have truncation
        applied. Should be `'right'` or `'left'`.

    Moreover, methods common to tokenizers for tokenization, token/id conversion
    and encoding as model inputs are also provided here.

    Besides, metaclass `InitTrackerMeta` is used to create `PretrainedTokenizer`,
    by which subclasses can track arguments for initialization automatically
    and expose special tokens initialization used as attributes.
    """

    added_tokens_encoder: Dict[str, int] = {}
    added_tokens_decoder: Dict[int, str] = {}
    unique_no_split_tokens: List[str] = []
    tokens_trie = Trie()

    _decode_use_source_tokenizer = False

    def _pre_init(self, original_init, *args, **kwargs):
        """
        It would be hooked before `__init__` to add specials tokens (arguments of
        `__init__` whose name ends with `_token`) as attributes of the tokenizer
        instance.
        """
        init_dict = fn_args_to_dict(original_init, *((self,) + args), **kwargs)
        init_dict.pop('self', None)
        super(PretrainedTokenizer, self).__init__(**init_dict)

        self.added_tokens_encoder: Dict[str, int] = {}
        self.added_tokens_decoder: Dict[int, str] = {}
        self.unique_no_split_tokens: List[str] = []
        self.tokens_trie = Trie()

        self._decode_use_source_tokenizer = False

    def _build_special_tokens_map_extended(self, **kwargs):
        for key, value in kwargs.items():
            if value is None:
                continue
            if key in self.SPECIAL_TOKENS_ATTRIBUTES:
                if key == "additional_special_tokens":
                    assert isinstance(
                        value,
                        (list, tuple)), f"Value {value} is not a list or tuple"
                    assert all(
                        isinstance(t, (str, AddedToken)) for t in value
                    ), "One of the tokens is not a string or an AddedToken"
                    setattr(self, key, value)
                elif isinstance(value, (str, AddedToken)):
                    setattr(self, key, value)
                else:
                    raise TypeError(
                        f"special token {key} has to be either str or AddedToken but got: {type(value)}"
                    )

    @property
    def vocab_size(self) -> int:
        """
        `int`: Size of the base vocabulary (without the added tokens).
        """
        raise NotImplementedError

    @property
    def is_fast(self) -> bool:
        return False

    def get_added_vocab(self) -> Dict[str, int]:
        """
        Returns the added tokens in the vocabulary as a dictionary of token to index.

        Returns:
            `Dict[str, int]`: The added tokens.
        """
        return self.added_tokens_encoder

    def __len__(self):
        """
        Size of the full vocabulary with the added tokens.
        """
        return self.vocab_size + len(self.added_tokens_encoder)

    def _add_tokens(self,
                    new_tokens: Union[List[str], List[AddedToken]],
                    special_tokens: bool = False) -> int:
        """
        Add a list of new tokens to the tokenizer class. If the new tokens are not in the vocabulary, they are added to
        it with indices starting from length of the current vocabulary.

        Args:
            new_tokens (`List[str]`or `List[AddedToken]`):
                Token(s) to add in vocabulary. A token is only added if it's not already in the vocabulary (tested by
                checking if the tokenizer assign the index of the `unk_token` to them).
            special_tokens (`bool`, *optional*, defaults to `False`):
                Whether or not the tokens should be added as special tokens.

        Returns:
            `int`: The number of tokens actually added to the vocabulary.

        Examples:

        ```python
        # Let's see how to increase the vocabulary of Bert model and tokenizer
        tokenizer = BertTokenizer.from_pretrained("bert-base-uncased")
        model = BertModel.from_pretrained("bert-base-uncased")

        num_added_toks = tokenizer.add_tokens(["new_tok1", "my_new-tok2"])
        print("We have added", num_added_toks, "tokens")
        ```"""
        new_tokens = [str(tok) for tok in new_tokens]

        tokens_to_add = []
        for token in new_tokens:
            if not isinstance(token, str):
                raise TypeError(
                    f"Token {token} is not a string but a {type(token)}.")
            if not special_tokens and hasattr(
                    self, "do_lower_case") and self.do_lower_case:
                token = token.lower()
            if (token != self.unk_token and self.convert_tokens_to_ids(token)
                    == self.convert_tokens_to_ids(self.unk_token)
                    and token not in tokens_to_add):
                tokens_to_add.append(token)
                if self.verbose:
                    logger.info(f"Adding {token} to the vocabulary")

        added_tok_encoder = dict(
            (tok, len(self) + i) for i, tok in enumerate(tokens_to_add))
        added_tok_decoder = {v: k for k, v in added_tok_encoder.items()}
        self.added_tokens_encoder.update(added_tok_encoder)
        self.added_tokens_decoder.update(added_tok_decoder)

        # Make sure we don't split on any special tokens (even they were already in the vocab before e.g. for Albert)
        if special_tokens:
            if len(new_tokens) == 1:
                _insert_one_token_to_ordered_list(self.unique_no_split_tokens,
                                                  new_tokens[0])
            else:
                self.unique_no_split_tokens = sorted(
                    set(self.unique_no_split_tokens).union(set(new_tokens)))
        else:
            # Or on the newly added tokens
            if len(tokens_to_add) == 1:
                _insert_one_token_to_ordered_list(self.unique_no_split_tokens,
                                                  tokens_to_add[0])
            else:
                self.unique_no_split_tokens = sorted(
                    set(self.unique_no_split_tokens).union(set(tokens_to_add)))
        self._create_trie(self.unique_no_split_tokens)

        return len(tokens_to_add)

    def _create_trie(self, unique_no_split_tokens):
        trie = Trie()
        for token in unique_no_split_tokens:
            if hasattr(
                    self, "do_lower_case"
            ) and self.do_lower_case and token not in self.all_special_tokens:
                trie.add(token.lower())
            else:
                trie.add(token)
        self.tokens_trie = trie

    def prepare_for_tokenization(self,
                                 text,
                                 is_split_into_words=False,
                                 **kwargs):
        """
        Performs any necessary transformations before tokenization.

        This method should pop the arguments from kwargs and return the remaining `kwargs` as well. We test the
        `kwargs` at the end of the encoding process to be sure all the arguments have been used.

        Args:
            text (`str`):
                The text to prepare.
            is_split_into_words (`bool`, *optional*, defaults to `False`):
                Whether or not the input is already pre-tokenized (e.g., split into words). If set to `True`, the
                tokenizer assumes the input is already split into words (for instance, by splitting it on whitespace)
                which it will tokenize. This is useful for NER or token classification.
            kwargs:
                Keyword arguments to use for the tokenization.

        Returns:
            `Tuple[str, Dict[str, Any]]`: The prepared text and the unused kwargs.
        """

        return (text, kwargs)

    def tokenize(self, text: TextInput, **kwargs) -> List[str]:
        """
        Converts a string in a sequence of tokens, using the tokenizer.

        Split in words for word-based vocabulary or sub-words for sub-word-based vocabularies
        (BPE/SentencePieces/WordPieces). Takes care of added tokens.

        Args:
            text (`str`):
                The sequence to be encoded.
            **kwargs (additional keyword arguments):
                Passed along to the model-specific `prepare_for_tokenization` preprocessing method.

        Returns:
            `List[str]`: The list of tokens.
        """
        # Simple mapping string => AddedToken for special tokens with specific tokenization behaviors
        all_special_tokens_extended = dict(
            (str(t), t) for t in self.all_special_tokens_extended
            if isinstance(t, AddedToken))

        text, kwargs = self.prepare_for_tokenization(text, **kwargs)

        # TODO: should this be in the base class?
        if hasattr(self, "do_lower_case") and self.do_lower_case:
            # convert non-special tokens to lowercase
            escaped_special_toks = [
                re.escape(s_tok) for s_tok in (self.unique_no_split_tokens +
                                               self.all_special_tokens)
            ]
            pattern = r"(" + r"|".join(escaped_special_toks) + r")|" + r"(.+?)"
            text = re.sub(pattern,
                          lambda m: m.groups()[0] or m.groups()[1].lower(),
                          text)

        no_split_token = set(self.unique_no_split_tokens)
        tokens = self.tokens_trie.split(text)

        # ["This is something", "<special_token_1>", "  else"]
        for i, token in enumerate(tokens):
            if token in no_split_token:
                tok_extended = all_special_tokens_extended.get(token, None)
                left = tokens[i - 1] if i > 0 else None
                right = tokens[i + 1] if i < len(tokens) - 1 else None
                if isinstance(tok_extended, AddedToken):
                    if tok_extended.rstrip and right:
                        # A bit counter-intuitive but we strip the left of the string
                        # since tok_extended.rstrip means the special token is eating all white spaces on its right
                        tokens[i + 1] = right.lstrip()
                    # Strip white spaces on the left
                    if tok_extended.lstrip and left:
                        tokens[i - 1] = left.rstrip()  # Opposite here
                else:
                    # We strip left and right by default
                    if right:
                        tokens[i + 1] = right.lstrip()
                    if left:
                        tokens[i - 1] = left.rstrip()
        # ["This is something", "<special_token_1>", "else"]
        tokenized_text = []
        for token in tokens:
            # Need to skip eventual empty (fully stripped) tokens
            if not token:
                continue
            if token in no_split_token:
                tokenized_text.append(token)
            else:
                tokenized_text.extend(self._tokenize(token))
        # ["This", " is", " something", "<special_token_1>", "else"]
        return tokenized_text

    def _tokenize(self, text, **kwargs):
        """
        Converts a string in a sequence of tokens (string), using the tokenizer. Split in words for word-based
        vocabulary or sub-words for sub-word-based vocabularies (BPE/SentencePieces/WordPieces).

        Do NOT take care of added tokens.
        """
        raise NotImplementedError

    def convert_tokens_to_ids(self, tokens):
        if tokens is None:
            return None

        if isinstance(tokens, str):
            return self._convert_token_to_id_with_added_voc(tokens)

        ids = []
        for token in tokens:
            ids.append(self._convert_token_to_id_with_added_voc(token))

        return ids

    def _convert_token_to_id_with_added_voc(self, token):
        if token is None:
            return None

        if token in self.added_tokens_encoder:
            return self.added_tokens_encoder[token]
        return self._convert_token_to_id(token)

    def _convert_token_to_id(self, token):

        return self.vocab.to_indices(token)

    def convert_tokens_to_string(self, tokens):
        """
        Converts a sequence of tokens (list of string) to a single string by
        using ``' '.join(tokens)`` .

        Args:
            tokens (list[str]): A sequence of tokens.

        Returns:
            str: Converted string.
        """
        return " ".join(tokens)

    def convert_ids_to_tokens(self, ids, skip_special_tokens=False):
        if isinstance(ids, int):
            if ids in self.added_tokens_decoder:
                return self.added_tokens_decoder[ids]
            else:
                return self._convert_id_to_token(ids)
        tokens = []
        for index in ids:
            index = int(index)
            if skip_special_tokens and index in self.all_special_ids:
                continue
            if index in self.added_tokens_decoder:
                tokens.append(self.added_tokens_decoder[index])
            else:
                tokens.append(self._convert_id_to_token(index))
        return tokens

    def _convert_id_to_token(self, index):

        return self.vocab.to_tokens(index)

    @staticmethod
    def load_vocabulary(filepath,
                        unk_token=None,
                        pad_token=None,
                        bos_token=None,
                        eos_token=None,
                        **kwargs):
        """
        Instantiate an instance of `Vocab` from a file reserving all tokens
        by using `Vocab.from_dict`. The file contains a token per line, and the
        line number would be the index of corresponding token.

        Args:
            filepath (str): path of file to construct vocabulary.
            unk_token (str): special token for unknown token. If no need, it also
                could be `None`. Defaults to `None`.
            pad_token (str): special token for padding token. If no need, it also
                could be `None`. Defaults to `None`.
            bos_token (str): special token for bos token. If no need, it also
                could be `None`. Defaults to `None`.
            eos_token (str): special token for eos token. If no need, it also
                could be `None`. Defaults to `None`.
            **kwargs (dict): keyword arguments for `Vocab.from_dict`.

        Returns:
            Vocab: An instance of `Vocab`.
        """
        token_to_idx = {}
        with io.open(filepath, 'r', encoding='utf-8') as f:
            for index, line in enumerate(f):
                token = line.rstrip('\n')
                token_to_idx[token] = int(index)
        vocab = Vocab.from_dict(token_to_idx,
                                unk_token=unk_token,
                                pad_token=pad_token,
                                bos_token=bos_token,
                                eos_token=eos_token,
                                **kwargs)
        return vocab

    @staticmethod
    def save_vocabulary(filepath, vocab):
        """
        Save all tokens to a vocabulary file. The file contains a token per line,
        and the line number would be the index of corresponding token.

        Args:
            filepath (str): File path to be saved to.
            vocab (Vocab|dict): The `Vocab` or `dict` instance to be saved.
        """
        if isinstance(vocab, Vocab):
            tokens = vocab.idx_to_token
        else:
            tokens = sorted(vocab.keys(), key=lambda token: vocab[token])
        with io.open(filepath, 'w', encoding='utf-8') as f:
            for token in tokens:
                f.write(token + '\n')

    def get_special_tokens_mask(self,
                                token_ids_0,
                                token_ids_1=None,
                                already_has_special_tokens=False):
        """
        Retrieves sequence ids from a token list that has no special tokens added. This method is called when adding
        special tokens using the tokenizer ``encode`` methods.

        Args:
            token_ids_0 (List[int]): List of ids of the first sequence.
            token_ids_1 (List[int], optional): List of ids of the second sequence.
            already_has_special_tokens (bool, optional): Whether or not the token list is already
                formatted with special tokens for the model. Defaults to None.

        Returns:
            results (List[int]): The list of integers in the range [0, 1]:
                1 for a special token, 0 for a sequence token.
        """
        if already_has_special_tokens:
            if token_ids_1 is not None:
                raise ValueError(
                    "You should not supply a second sequence if the provided sequence of "
                    "ids is already formatted with special tokens for the model."
                )

            return super().get_special_tokens_mask(
                token_ids_0=token_ids_0,
                token_ids_1=token_ids_1,
                already_has_special_tokens=True)
        return [0] * (
                (len(token_ids_1) if token_ids_1 else 0) + len(token_ids_0))

    def num_special_tokens_to_add(self, pair):
        """
        Returns the number of added tokens when encoding a sequence with special tokens.

        Args:
            pair (bool, optional):
                Whether the number of added tokens should be computed in the case of a sequence pair or a single
                sequence. Defaults to `False`.
        Returns:
            int: Number of special tokens added to sequences.
        """
        token_ids_0 = []
        token_ids_1 = []
        return len(
            self.build_inputs_with_special_tokens(
                token_ids_0, token_ids_1 if pair else None))

    def _encode_plus(
            self,
            text: Union[TextInput, PreTokenizedInput, EncodedInput],
            text_pair: Optional[Union[TextInput, PreTokenizedInput,
                                      EncodedInput]] = None,
            add_special_tokens: bool = True,
            padding_strategy: PaddingStrategy = PaddingStrategy.DO_NOT_PAD,
            truncation_strategy: TruncationStrategy = TruncationStrategy.
                DO_NOT_TRUNCATE,
            max_length: Optional[int] = None,
            stride: int = 0,
            is_split_into_words: bool = False,
            pad_to_multiple_of: Optional[int] = None,
            return_tensors: Optional[Union[str, TensorType]] = None,
            return_position_ids: Optional[bool] = None,
            return_token_type_ids: Optional[bool] = None,
            return_attention_mask: Optional[bool] = None,
            return_overflowing_tokens: bool = False,
            return_special_tokens_mask: bool = False,
            return_offsets_mapping: bool = False,
            return_length: bool = False,
            verbose: bool = True,
            **kwargs) -> BatchEncoding:

        def get_input_ids(text):
            if isinstance(text, str):
                tokens = self.tokenize(text, **kwargs)
                return self.convert_tokens_to_ids(tokens)
            elif isinstance(text,
                            (list, tuple)) and len(text) > 0 and isinstance(
                text[0], str):
                if is_split_into_words == True:
                    tokens = list(
                        itertools.chain(*(
                            self.tokenize(t, is_split_into_words=True, **kwargs)
                            for t in text)))
                    return self.convert_tokens_to_ids(tokens)
                else:
                    return self.convert_tokens_to_ids(text)
            elif isinstance(text,
                            (list, tuple)) and len(text) > 0 and isinstance(
                text[0], int):
                return text
            else:
                if is_split_into_words:
                    raise ValueError(
                        f"Input {text} is not valid. Should be a string or a list/tuple of strings when `is_split_into_words=True`."
                    )
                else:
                    raise ValueError(
                        f"Input {text} is not valid. Should be a string, a list/tuple of strings or a list/tuple of integers."
                    )

        first_ids = get_input_ids(text)
        second_ids = get_input_ids(text_pair) if text_pair is not None else None

        encoded_inputs = self.prepare_for_model(
            first_ids,
            pair_ids=second_ids,
            add_special_tokens=add_special_tokens,
            padding=padding_strategy.value,
            truncation=truncation_strategy.value,
            max_length=max_length,
            stride=stride,
            pad_to_multiple_of=pad_to_multiple_of,
            return_tensors=return_tensors,
            prepend_batch_axis=True,
            return_position_ids=return_position_ids,
            return_attention_mask=return_attention_mask,
            return_token_type_ids=return_token_type_ids,
            return_overflowing_tokens=return_overflowing_tokens,
            return_special_tokens_mask=return_special_tokens_mask,
            return_offsets_mapping=return_offsets_mapping,
            return_length=return_length,
            verbose=verbose,
            **kwargs)

        if return_offsets_mapping:
            pair = bool(second_ids is not None)
            len_ids = len(first_ids)
            len_pair_ids = len(second_ids) if pair else 0
            total_len = len_ids + len_pair_ids + (
                self.num_special_tokens_to_add(pair=pair) if add_special_tokens else 0)

            token_offset_mapping = self.get_offset_mapping(text)
            token_pair_offset_mapping = self.get_offset_mapping(text_pair) if text_pair is not None else None
            if max_length and total_len > max_length:
                token_offset_mapping, token_pair_offset_mapping, _ = self.truncate_sequences(
                    token_offset_mapping,
                    pair_ids=token_pair_offset_mapping,
                    num_tokens_to_remove=total_len - max_length,
                    truncation_strategy=truncation_strategy,
                    stride=stride,
                )
            if add_special_tokens:
                offset_mapping = self.build_offset_mapping_with_special_tokens(
                    token_offset_mapping, token_pair_offset_mapping
                )
            else:
                offset_mapping = (
                    token_offset_mapping + token_pair_offset_mapping
                    if token_pair_offset_mapping
                    else token_offset_mapping
                )
            encoded_inputs["offset_mapping"] = offset_mapping

        return encoded_inputs

    def _batch_encode_plus(
            self,
            batch_text_or_text_pairs: Union[List[TextInput],
                                            List[TextInputPair],
                                            List[PreTokenizedInput],
                                            List[PreTokenizedInputPair],
                                            List[EncodedInput],
                                            List[EncodedInputPair],],
            add_special_tokens: bool = True,
            padding_strategy: PaddingStrategy = PaddingStrategy.DO_NOT_PAD,
            truncation_strategy: TruncationStrategy = TruncationStrategy.
                DO_NOT_TRUNCATE,
            max_length: Optional[int] = None,
            stride: int = 0,
            is_split_into_words: bool = False,
            pad_to_multiple_of: Optional[int] = None,
            return_position_ids: Optional[bool] = None,
            return_tensors: Optional[Union[str, TensorType]] = None,
            return_token_type_ids: Optional[bool] = None,
            return_attention_mask: Optional[bool] = None,
            return_overflowing_tokens: bool = False,
            return_special_tokens_mask: bool = False,
            return_dict: bool = True,
            return_offsets_mapping: bool = False,
            return_length: bool = False,
            verbose: bool = True,
            **kwargs) -> BatchEncoding:

        def get_input_ids(text):
            if isinstance(text, str):
                tokens = self.tokenize(text, **kwargs)
                return self.convert_tokens_to_ids(tokens)
            elif isinstance(text,
                            (list, tuple)) and len(text) > 0 and isinstance(
                text[0], str):
                if is_split_into_words == True:
                    tokens = list(
                        itertools.chain(*(
                            self.tokenize(t, is_split_into_words=True, **kwargs)
                            for t in text)))
                    return self.convert_tokens_to_ids(tokens)
                else:
                    return self.convert_tokens_to_ids(text)
            elif isinstance(text,
                            (list, tuple)) and len(text) > 0 and isinstance(
                text[0], int):
                return text
            else:
                raise ValueError(
                    "Input is not valid. Should be a string, a list/tuple of strings or a list/tuple of integers."
                )

        input_ids = []
        for ids_or_pair_ids in batch_text_or_text_pairs:
            if not isinstance(ids_or_pair_ids, (list, tuple)):
                ids, pair_ids = ids_or_pair_ids, None
            elif is_split_into_words and not isinstance(ids_or_pair_ids[0],
                                                        (list, tuple)):
                ids, pair_ids = ids_or_pair_ids, None
            else:
                ids, pair_ids = ids_or_pair_ids

            first_ids = get_input_ids(ids)
            second_ids = get_input_ids(
                pair_ids) if pair_ids is not None else None
            input_ids.append((first_ids, second_ids))

        if stride > 0 and second_ids is not None:
            kwargs['batch_text_or_text_pairs'] = batch_text_or_text_pairs
        else:
            if return_offsets_mapping:
                has_pair = False
                if len(batch_text_or_text_pairs) > 0:
                    if isinstance(batch_text_or_text_pairs[0], (list, tuple)):
                        has_pair = True
                kwargs['texts'] = None
                kwargs['text_pairs'] = None
                if has_pair:
                    kwargs['texts'] = [
                        text[0] for text in batch_text_or_text_pairs
                    ]
                    kwargs['text_pairs'] = [
                        text[1] for text in batch_text_or_text_pairs
                    ]
                else:
                    kwargs['texts'] = [
                        text for text in batch_text_or_text_pairs
                    ]

        batch_outputs = self._batch_prepare_for_model(
            input_ids,
            add_special_tokens=add_special_tokens,
            padding_strategy=padding_strategy,
            truncation_strategy=truncation_strategy,
            max_length=max_length,
            stride=stride,
            pad_to_multiple_of=pad_to_multiple_of,
            return_position_ids=return_position_ids,
            return_attention_mask=return_attention_mask,
            return_token_type_ids=return_token_type_ids,
            return_overflowing_tokens=return_overflowing_tokens,
            return_special_tokens_mask=return_special_tokens_mask,
            return_dict=return_dict,
            return_offsets_mapping=return_offsets_mapping,
            return_length=return_length,
            return_tensors=return_tensors,
            verbose=verbose,
            **kwargs)

        return batch_outputs

    def _batch_prepare_for_model(
            self,
            batch_ids_pairs: List[Union[PreTokenizedInputPair, Tuple[List[int],
                                                                     None]]],
            add_special_tokens: bool = True,
            padding_strategy: PaddingStrategy = PaddingStrategy.DO_NOT_PAD,
            truncation_strategy: TruncationStrategy = TruncationStrategy.
                DO_NOT_TRUNCATE,
            max_length: Optional[int] = None,
            stride: int = 0,
            pad_to_multiple_of: Optional[int] = None,
            return_position_ids: Optional[bool] = None,
            return_tensors: Optional[str] = None,
            return_token_type_ids: Optional[bool] = None,
            return_attention_mask: Optional[bool] = None,
            return_overflowing_tokens: bool = False,
            return_special_tokens_mask: bool = False,
            return_dict: bool = True,
            return_offsets_mapping: bool = False,
            return_length: bool = False,
            verbose: bool = True,
            **kwargs) -> BatchEncoding:
        """
        Prepares a sequence of input id, or a pair of sequences of inputs ids so that it can be used by the model. It
        adds special tokens, truncates sequences if overflowing while taking into account the special tokens and
        manages a moving window (with user defined stride) for overflowing tokens

        Args:
            batch_ids_pairs: list of tokenized input ids or input ids pairs
        """
        if return_token_type_ids and not add_special_tokens:
            raise ValueError(
                "Asking to return token_type_ids while setting add_special_tokens to False "
                "results in an undefined behavior. Please set add_special_tokens to True or "
                "set return_token_type_ids to None.")

        batch_outputs = {}
        batch_outputs_list = []
        for example_id, (first_ids, second_ids) in enumerate(batch_ids_pairs):
            if stride > 0 and second_ids is not None:
                if return_token_type_ids is None:
                    return_token_type_ids = "token_type_ids" in self.model_input_names
                if return_attention_mask is None:
                    return_attention_mask = "attention_mask" in self.model_input_names

                max_len_for_pair = max_length - len(first_ids) - (
                    self.num_special_tokens_to_add(
                        pair=True) if add_special_tokens else 0)

                text, text_pair = kwargs['batch_text_or_text_pairs'][example_id]
                token_offset_mapping = self.get_offset_mapping(text)
                token_pair_offset_mapping = self.get_offset_mapping(text_pair)

                offset = 0
                while offset < len(second_ids):
                    encoded_inputs = {}
                    length = len(second_ids) - offset
                    if length > max_len_for_pair:
                        length = max_len_for_pair

                    ids = first_ids
                    pair_ids = second_ids[offset:offset + length]
                    pair = bool(pair_ids is not None)
                    mapping = token_offset_mapping
                    pair_mapping = token_pair_offset_mapping[offset:offset +
                                                                    length]
                    if add_special_tokens:
                        offset_mapping = self.build_offset_mapping_with_special_tokens(
                            mapping, pair_mapping)
                        sequence = self.build_inputs_with_special_tokens(
                            ids, pair_ids)
                        token_type_ids = self.create_token_type_ids_from_sequences(
                            ids, pair_ids)
                    else:
                        offset_mapping = mapping + pair_mapping
                        sequence = ids + pair_ids if pair else ids
                        token_type_ids = [0] * len(ids) + ([0] * len(pair_ids)
                                                           if pair else [])
                    encoded_inputs['offset_mapping'] = offset_mapping
                    # Build output dictionnary
                    encoded_inputs["input_ids"] = sequence
                    if return_token_type_ids:
                        encoded_inputs["token_type_ids"] = token_type_ids
                    if return_special_tokens_mask:
                        if add_special_tokens:
                            encoded_inputs[
                                "special_tokens_mask"] = self.get_special_tokens_mask(
                                ids, pair_ids)
                        else:
                            encoded_inputs["special_tokens_mask"] = [
                                                                        0
                                                                    ] * len(sequence)

                    # Check lengths
                    self._eventual_warn_about_too_long_sequence(
                        encoded_inputs["input_ids"], max_length, verbose)
                    if return_position_ids:
                        encoded_inputs["position_ids"] = list(
                            range(len(encoded_inputs["input_ids"])))

                    if return_length:
                        encoded_inputs["length"] = len(
                            encoded_inputs["input_ids"])
                        encoded_inputs["seq_len"] = encoded_inputs["length"]

                    encoded_inputs['overflow_to_sample'] = example_id

                    for key, value in encoded_inputs.items():
                        if key not in batch_outputs:
                            batch_outputs[key] = []
                        batch_outputs[key].append(value)

                    if offset + length == len(second_ids):
                        break
                    offset += min(length, stride)
            else:
                if return_offsets_mapping:
                    kwargs['text'] = kwargs['texts'][example_id]
                    kwargs['text_pair'] = None
                    if kwargs['text_pairs'] is not None:
                        kwargs['text_pair'] = kwargs['text_pairs'][example_id]

                encoded_inputs = self.prepare_for_model(
                    first_ids,
                    second_ids,
                    add_special_tokens=add_special_tokens,
                    padding=PaddingStrategy.DO_NOT_PAD.
                        value,  # we pad in batch afterward
                    truncation=truncation_strategy.value,
                    max_length=max_length,
                    stride=stride,
                    pad_to_multiple_of=None,  # we pad in batch afterward
                    return_position_ids=
                    return_position_ids,  # we pad in batch afterward
                    return_attention_mask=False,  # we pad in batch afterward
                    return_token_type_ids=return_token_type_ids,
                    return_overflowing_tokens=return_overflowing_tokens,
                    return_special_tokens_mask=return_special_tokens_mask,
                    return_offsets_mapping=return_offsets_mapping,
                    return_length=return_length,
                    return_tensors=
                    None,  # We convert the whole batch to tensors at the end
                    prepend_batch_axis=False,
                    verbose=verbose,
                    **kwargs)
                for key, value in encoded_inputs.items():
                    if key not in batch_outputs:
                        batch_outputs[key] = []
                    batch_outputs[key].append(value)

        batch_outputs = self.pad(
            batch_outputs,
            padding=padding_strategy.value,
            max_length=max_length,
            pad_to_multiple_of=pad_to_multiple_of,
            return_attention_mask=return_attention_mask,
        )
        if return_dict:
            batch_outputs = BatchEncoding(batch_outputs,
                                          tensor_type=return_tensors)
            return batch_outputs
        else:
            for k, v in batch_outputs.items():
                for i in range(len(v)):
                    if i >= len(batch_outputs_list):
                        batch_outputs_list.append({k: v[i]})
                    else:
                        batch_outputs_list[i][k] = v[i]
            return batch_outputs_list

    def get_offset_mapping(self, text):
        """
        Returns the map of tokens and the start and end index of their start and end character.
        Modified from https://github.com/bojone/bert4keras/blob/master/bert4keras/tokenizers.py#L372

        Args:
            text (str):
                Input text.
        Returns:
            list: The offset map of input text.

        """
        if text is None:
            return None
        split_tokens = []
        if hasattr(self, "basic_tokenizer"):
            for token in self.basic_tokenizer.tokenize(
                    text, never_split=self.all_special_tokens):
                # If the token is part of the never_split set
                if token in self.basic_tokenizer.never_split:
                    split_tokens.append(token)
                else:
                    for sub_token in self.wordpiece_tokenizer.tokenize(token):
                        split_tokens.append(
                            sub_token if sub_token != self.unk_token else token)
        else:
            for sub_token in self.wordpiece_tokenizer.tokenize(text):
                split_tokens.append(
                    sub_token if sub_token != self.unk_token else text)

        normalized_text, char_mapping = '', []

        for i, ch in enumerate(text):
            if hasattr(self, "do_lower_case") and self.do_lower_case:
                ch = ch.lower()
                if self.basic_tokenizer.strip_accents is not False:
                    ch = unicodedata.normalize('NFD', ch)
                    ch = ''.join(
                        [c for c in ch if unicodedata.category(c) != 'Mn'])
            elif self.basic_tokenizer.strip_accents:
                ch = unicodedata.normalize('NFD', ch)
                ch = ''.join([c for c in ch if unicodedata.category(c) != 'Mn'])

            ch = ''.join([
                c for c in ch
                if not (ord(c) == 0 or ord(c) == 0xfffd or _is_control(c))
            ])
            normalized_text += ch

            char_mapping.extend([i] * len(ch))
        text, token_mapping, offset = normalized_text, [], 0

        for token in split_tokens:
            if token[:2] == '##':
                token = token[2:]
            if token in self.all_special_tokens:
                token = token.lower() if hasattr(
                    self, "do_lower_case") and self.do_lower_case else token
            # The greek letter "sigma" has 2 forms of lowercase, σ and ς respectively.
            # When used as a final letter of a word, the final form (ς) is used. Otherwise, the form (σ) is used.
            # https://latin.stackexchange.com/questions/6168/how-and-when-did-we-get-two-forms-of-sigma
            if "σ" in token or "ς" in token:
                start = text[offset:].replace("ς", "σ").index(
                    token.replace("ς", "σ")) + offset
            else:
                start = text[offset:].index(token) + offset

            end = start + len(token)

            token_mapping.append(
                (char_mapping[start], char_mapping[end - 1] + 1))
            offset = end

        return token_mapping

    def _decode(self,
                token_ids: List[int],
                skip_special_tokens: bool = False,
                clean_up_tokenization_spaces: bool = True,
                spaces_between_special_tokens: bool = True,
                **kwargs) -> str:
        self._decode_use_source_tokenizer = kwargs.pop("use_source_tokenizer",
                                                       False)

        filtered_tokens = self.convert_ids_to_tokens(
            token_ids, skip_special_tokens=skip_special_tokens)

        # To avoid mixing byte-level and unicode for byte-level BPT
        # we need to build string separately for added tokens and byte-level tokens
        # cf. https://github.com/huggingface/transformers/issues/1133
        sub_texts = []
        current_sub_text = []
        for token in filtered_tokens:
            if skip_special_tokens and token in self.all_special_ids:
                continue
            if token in self.added_tokens_encoder:
                if current_sub_text:
                    sub_texts.append(
                        self.convert_tokens_to_string(current_sub_text))
                    current_sub_text = []
                sub_texts.append(token)
            else:
                current_sub_text.append(token)
        if current_sub_text:
            sub_texts.append(self.convert_tokens_to_string(current_sub_text))

        if spaces_between_special_tokens:
            text = " ".join(sub_texts)
        else:
            text = "".join(sub_texts)

        if clean_up_tokenization_spaces:
            clean_text = self.clean_up_tokenization(text)
            return clean_text
        else:
            return text
