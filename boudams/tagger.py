import torch
import torch.cuda
import dill

from torchtext.data import ReversibleField, BucketIterator

import os
import json
import tarfile
from typing import List, Tuple

from .model import gru, lstm, bidir, conv
from .dataset import build_vocab, CharacterField, TabularDataset as Dataset, get_datasets, InputDataset
from . import utils

from .encoder import LabelEncoder, DatasetIterator


teacher_forcing_ratio = 0.5

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
MAX_LENGTH = 150


class Seq2SeqTokenizer:
    def __init__(
            self,
            vocabulary: LabelEncoder,
            hidden_size: int = 256,
            enc_n_layers: int = 10, dec_n_layers: int = 10,
            emb_enc_dim: int = 256, emb_dec_dim: int = 256,
            enc_hid_dim: int = None, dec_hid_dim: int = None,
            enc_dropout: float = 0.5, dec_dropout: float = 0.5,
            enc_kernel_size: int = 3, dec_kernel_size: int = 3,
            out_max_sentence_length: int = 150,
            device: str = DEVICE, system: str = "bi-gru",
            **kwargs # RetroCompat
    ):
        """

        :param vocabulary:
        :param hidden_size:
        :param n_layers:
        :param emb_enc_dim:
        :param emb_dec_dim:
        :param max_length:
        :param device:
        """

        self.vocabulary: LabelEncoder = vocabulary
        self.vocabulary_dimension: int = len(self.vocabulary)

        self.device: str = device
        self.enc_hid_dim = self.dec_hid_dim = self.hidden_size = hidden_size

        if enc_hid_dim and dec_hid_dim:
            self.enc_hid_dim: int = enc_hid_dim
            self.dec_hid_dim: int = dec_hid_dim

        self.emb_enc_dim: int = emb_enc_dim
        self.emb_dec_dim: int = emb_dec_dim
        self.enc_dropout: float = enc_dropout
        self.dec_dropout: float = dec_dropout
        self.enc_kernel_size: int = enc_kernel_size
        self.dec_kernel_size: int = dec_kernel_size
        self.enc_n_layers: int = enc_n_layers
        self.dec_n_layers: int = dec_n_layers

        self.out_max_sentence_length: int = out_max_sentence_length
        self.system: str = system

        seq2seq_shared_params = {
            "pad_idx": self.padtoken,
            "sos_idx": self.sostoken,
            "eos_idx": self.eostoken,
            "device": self.device,
            "out_max_sentence_length": self.out_max_sentence_length
        }

        if self.system == "conv":
            self.enc: gru.Encoder = conv.Encoder(
                self.vocabulary_dimension, emb_dim=self.emb_enc_dim,
                n_layers=self.enc_n_layers, hid_dim=self.enc_hid_dim,
                dropout=self.enc_dropout,
                device=self.device,
                kernel_size=self.enc_kernel_size
            )
            self.dec: gru.Decoder = conv.Decoder(
                self.vocabulary_dimension, emb_dim=self.emb_dec_dim,
                hid_dim=self.dec_hid_dim, dropout=self.enc_dropout,
                device=self.device, pad_idx=self.padtoken, kernel_size=self.enc_kernel_size,
                n_layers=self.dec_n_layers, max_sentence_len=self.out_max_sentence_length
            )
            self.init_weights = None
            self.model: gru.Seq2Seq = conv.Seq2Seq(self.enc, self.dec, **seq2seq_shared_params).to(device)
        elif self.system == "gru":
            self.enc: gru.Encoder = gru.Encoder(self.vocabulary_dimension, self.emb_enc_dim, self.hidden_size,
                                                self.enc_dropout)
            self.dec: gru.Decoder = gru.Decoder(self.vocabulary_dimension, self.emb_dec_dim, self.hidden_size,
                                                self.dec_dropout)

            self.model: gru.Seq2Seq = gru.Seq2Seq(self.enc, self.dec, **seq2seq_shared_params).to(device)
            self.init_weights = gru.init_weights
        elif self.system == "bi-gru":
            self.enc: gru.Encoder = bidir.Encoder(
                self.vocabulary_dimension, emb_dim=self.emb_enc_dim,
                enc_hid_dim=self.enc_hid_dim, dec_hid_dim=self.dec_hid_dim, dropout=self.enc_dropout
            )
            self.attention: bidir.Attention = bidir.Attention(
                enc_hid_dim=self.enc_hid_dim, dec_hid_dim=self.dec_hid_dim
            )
            self.dec: gru.Decoder = bidir.Decoder(
                self.vocabulary_dimension, emb_dim=self.emb_dec_dim,
                enc_hid_dim=self.enc_hid_dim, dec_hid_dim=self.dec_hid_dim, dropout=self.enc_dropout,
                attention=self.attention
            )
            self.init_weights = bidir.init_weights
            self.model: gru.Seq2Seq = bidir.Seq2Seq(self.enc, self.dec, **seq2seq_shared_params).to(device)
        else:
            self.enc: lstm.Encoder = lstm.Encoder(self.vocabulary_dimension, self.emb_enc_dim, self.hidden_size,
                                                  self.enc_n_layers, self.enc_dropout)
            self.dec: lstm.Decoder = lstm.Decoder(self.vocabulary_dimension, self.emb_dec_dim, self.hidden_size,
                                                  self.dec_n_layers, self.dec_dropout)
            self.init_weights = lstm.init_weights
            self.model: lstm.Seq2Seq = lstm.Seq2Seq(self.enc, self.dec, **seq2seq_shared_params).to(device)

    @property
    def padtoken(self):
        return self.vocabulary.pad_token_index

    @property
    def sostoken(self):
        return self.vocabulary.init_token_index

    @property
    def eostoken(self):
        return self.vocabulary.eos_token_index

    def tag(self, iterator: BucketIterator):
        self.model.eval()
        for i, batch in enumerate(iterator):
            src, src_len = batch.src
            output, attention = self.model(
                src, src_len, trg=None,
                teacher_forcing_ratio=0
            )  # turn off teacher forcing

            # trg = [trg sent len, batch size]
            # output = [Maximum Sentence Length, Number of Sentence in batch, Number of possible characters]
            _, ind = torch.topk(output, 1, dim=2)
            # ind = [Maximum Sentence Length, Number of Sentences in Batch, One Result]

            # output = output[1:].view(-1, output.shape[-1])

            yield ind.squeeze().permute(1, 0)

    @property
    def settings(self):
        return {
            "enc_kernel_size": self.enc_kernel_size,
            "dec_kernel_size": self.dec_kernel_size,
            "enc_n_layers": self.enc_n_layers,
            "dec_n_layers": self.dec_n_layers,
            "hidden_size": self.hidden_size,
            "enc_hid_dim": self.enc_hid_dim,
            "dec_hid_dim": self.dec_hid_dim,
            "emb_enc_dim": self.emb_enc_dim,
            "emb_dec_dim": self.emb_dec_dim,
            "enc_dropout": self.enc_dropout,
            "dec_dropout": self.dec_dropout,
            "out_max_sentence_length": self.out_max_sentence_length,
            "system": self.system
        }

    @classmethod
    def load(cls, fpath="./model.tar"):
        with tarfile.open(utils.ensure_ext(fpath, 'tar'), 'r') as tar:
            settings = json.loads(utils.get_gzip_from_tar(tar, 'settings.json.zip'))

            # load state_dict
            with utils.tmpfile() as tmppath:
                tar.extract('vocabulary.json', path=tmppath)
                dictpath = os.path.join(tmppath, 'vocabulary.json')
                with open(dictpath) as f:
                    vocab = LabelEncoder.load(json.load(f))

            obj = cls(vocabulary=vocab, **settings)

            # load state_dict
            with utils.tmpfile() as tmppath:
                tar.extract('state_dict.pt', path=tmppath)
                dictpath = os.path.join(tmppath, 'state_dict.pt')
                obj.model.load_state_dict(torch.load(dictpath))

        obj.model.eval()

        return obj

    def _reverse_keep(self, batch, remove_index=True):
        # keep the tokens to ignore normally
        start_index = int(remove_index)
        if not self.vocabulary.batch_first:
            batch = batch.t()
        with torch.cuda.device_of(batch):
            batch = batch.tolist()
        batch = [
            [self.vocabulary.vocab.itos[ind] for ind in ex[start_index:]]
            for ex in batch
        ]  # denumericalize

        return [ex for ex in batch]

    def reverse(self, batch, as_list=False):
        if not self.vocabulary.batch_first:
            batch = batch.t()
        with torch.cuda.device_of(batch):
            batch = batch.tolist()
        ignore = (self.eostoken, self.sostoken, self.padtoken, self.vocabulary.unk_token)
        batch = [
            [self.vocabulary.vocab.itos[ind] for ind in ex[1:] if ind not in ignore]
            for ex in batch
        ]  # denumericalize

        return [''.join(ex) for ex in batch]

    def _reverse_loop(self, tensor):
        for t in tensor:
            if t == self.eostoken:
                break
            yield self.vocabulary.vocab.itos[t]

    def annotate(self, texts: List[str]):

        self.model.eval()
        for sentence in texts:
            tensor, sentence_length = self.vocabulary.tensorize([list(sentence)])

            tensor = self.model._reshape_input(tensor, None)
            translation_tensor_logits, attention = self.model(
                tensor, sentence_length,
                trg=None, teacher_forcing_ratio=0
            )

            translation_tensor = self.model._reshape_output_for_scorer(translation_tensor_logits)
            print(translation_tensor.shape)
            translation = list(self._reverse_loop(translation_tensor))[1:]
            if attention is not None:
                attention = attention[1:]
            yield "".join(translation)#, attention

