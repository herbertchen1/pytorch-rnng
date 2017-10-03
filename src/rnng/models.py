from collections import OrderedDict, namedtuple
from typing import Collection, Mapping, Sequence, Sized, Tuple
from typing import Dict, List  # noqa

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
from torch.autograd import Variable

from rnng.typing import WordId, POSId, NTId, ActionId


class EmptyStackError(Exception):
    def __init__(self):
        super().__init__('stack is already empty')


class StackLSTM(nn.Module, Sized):
    BATCH_SIZE = 1
    SEQ_LEN = 1

    def __init__(self, input_size: int, hidden_size: int, num_layers: int = 1,
                 dropout: float = 0.) -> None:
        if num_layers < 1:
            raise ValueError('number of layers is at least 1')

        super().__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.dropout = dropout
        self.lstm = nn.LSTM(input_size, hidden_size, num_layers=num_layers, dropout=dropout)
        self.h0 = nn.Parameter(torch.Tensor(num_layers, self.BATCH_SIZE, hidden_size))
        self.c0 = nn.Parameter(torch.Tensor(num_layers, self.BATCH_SIZE, hidden_size))
        init_states = (self.h0, self.c0)
        self._states_hist = [init_states]
        self._outputs_hist = []  # type: List[Variable]

    def forward(self, inputs: Variable) -> Tuple[Variable, Variable]:
        # inputs: input_size
        assert self._states_hist

        # Set seq_len and batch_size to 1
        inputs = inputs.view(self.SEQ_LEN, self.BATCH_SIZE, inputs.numel())
        next_outputs, next_states = self.lstm(inputs, self._states_hist[-1])
        self._states_hist.append(next_states)
        self._outputs_hist.append(next_outputs)
        return next_states

    def push(self, *args, **kwargs):
        return self(*args, **kwargs)

    def pop(self) -> Tuple[Variable, Variable]:
        if len(self._states_hist) > 1:
            self._outputs_hist.pop()
            return self._states_hist.pop()
        else:
            raise EmptyStackError()

    @property
    def top(self) -> Variable:
        # outputs: hidden_size
        return self._outputs_hist[-1].squeeze() if self._outputs_hist else None

    def __repr__(self) -> str:
        res = ('{}(input_size={input_size}, hidden_size={hidden_size}, '
               'num_layers={num_layers}, dropout={dropout})')
        return res.format(self.__class__.__name__, **self.__dict__)

    def __len__(self):
        return len(self._outputs_hist)


def log_softmax(inputs: Variable, restrictions=None) -> Variable:
    if restrictions is None:
        return F.log_softmax(inputs)

    if restrictions.dim() != 1:
        raise RuntimeError('restrictions must be one dimensional')

    addend = Variable(
        inputs.data.new(inputs.size()).zero_().index_fill_(
            inputs.dim() - 1, restrictions, -float('inf')))
    return F.log_softmax(inputs + addend)


StackElement = namedtuple('StackElement', ['emb', 'is_open_nt'])


class DiscRNNGrammar(nn.Module):
    MAX_OPEN_NT = 100

    def __init__(self, num_words: int, num_pos: int, num_nt: int, num_actions: int,
                 shift_action: ActionId, action2nt: Mapping[ActionId, NTId],
                 word_dim: int = 32, pos_dim: int = 12, nt_dim: int = 60, action_dim: int = 16,
                 input_dim: int = 128, hidden_dim: int = 128, num_layers: int = 2,
                 dropout: float = .0) -> None:
        if shift_action in action2nt:
            raise ValueError('SHIFT action cannot also be NT(X) action')

        super().__init__()
        self.num_words = num_words
        self.num_pos = num_pos
        self.num_nt = num_nt
        self.num_actions = num_actions
        self.shift_action = shift_action
        self.action2nt = action2nt
        self.word_dim = word_dim
        self.pos_dim = pos_dim
        self.nt_dim = nt_dim
        self.action_dim = action_dim
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.num_layers = num_layers
        self.dropout = dropout

        # Parser states
        self._stack = []  # type: List[StackElement]
        self._buffer = []  # type: List[WordId]
        self._history = []  # type: List[ActionId]

        # Parser state encoders
        self.stack_lstm = StackLSTM(
            input_dim, hidden_dim, num_layers=num_layers, dropout=dropout)
        self.buffer_lstm = StackLSTM(  # can use LSTM, but this is easier
            input_dim, hidden_dim, num_layers=num_layers, dropout=dropout)
        self.history_lstm = StackLSTM(  # can use LSTM, but this is more efficient
            input_dim, hidden_dim, num_layers=num_layers, dropout=dropout)

        # Composition
        self.compose_fwd_lstm = nn.LSTM(
            input_dim, input_dim, num_layers=num_layers, dropout=dropout)
        self.compose_bwd_lstm = nn.LSTM(
            input_dim, input_dim, num_layers=num_layers, dropout=dropout)

        # Transformations
        self.word2lstm = nn.Sequential(OrderedDict([
            ('linear', nn.Linear(word_dim + pos_dim, input_dim)),
            ('relu', nn.ReLU())
        ]))
        self.nt2lstm = nn.Sequential(OrderedDict([
            ('linear', nn.Linear(nt_dim, input_dim)),
            ('relu', nn.ReLU())
        ]))
        self.action2lstm = nn.Sequential(OrderedDict([
            ('linear', nn.Linear(action_dim, input_dim)),
            ('relu', nn.ReLU())
        ]))
        self.compose2final = nn.Sequential(OrderedDict([
            ('linear', nn.Linear(2 * input_dim, input_dim)),
            ('relu', nn.ReLU())
        ]))
        self.lstms2summary = nn.Sequential(OrderedDict([  # Stack LSTMs to parser summary
            ('dropout', nn.Dropout(dropout)),
            ('linear', nn.Linear(3 * hidden_dim, hidden_dim)),
            ('relu', nn.ReLU())
        ]))
        self.summary2actions = nn.Linear(hidden_dim, num_actions)

        # Embeddings
        self.word_emb = nn.Embedding(num_words, word_dim)
        self.pos_emb = nn.Embedding(num_pos, pos_dim)
        self.nt_emb = nn.Embedding(num_nt, nt_dim)
        self.action_emb = nn.Embedding(num_actions, action_dim)

        # Guard parameters for stack, buffer, and action history
        self.stack_guard = nn.Parameter(torch.Tensor(input_dim))
        self.buffer_guard = nn.Parameter(torch.Tensor(input_dim))
        self.history_guard = nn.Parameter(torch.Tensor(input_dim))

        # Feed guards as inputs
        self.stack_lstm.push(self.stack_guard)
        self.buffer_lstm.push(self.buffer_guard)
        self.history_lstm.push(self.history_guard)

        # Final embeddings
        self._word_emb = {}  # type: Dict[WordId, Variable]
        self._nt_emb = {}  # type: Dict[NTId, Variable]
        self._action_emb = {}  # type: Dict[ActionId, Variable]

    @property
    def stack_buffer(self):
        return tuple(self._stack)

    @property
    def input_buffer(self):
        return tuple(reversed(self._buffer))

    @property
    def action_history(self):
        return tuple(self._history)

    @property
    def num_open_nt(self) -> int:
        return sum(tuple(zip(*self._stack))[1]) if self._stack else 0

    def start(self, tagged_words: Sequence[Tuple[WordId, POSId]]) -> None:
        self._stack = []
        self._buffer = []
        self._history = []

        while len(self.stack_lstm) > 1:
            self.stack_lstm.pop()
        while len(self.buffer_lstm) > 1:
            self.buffer_lstm.pop()
        while len(self.history_lstm) > 1:
            self.history_lstm.pop()

        words, pos_tags = tuple(zip(*tagged_words))
        self._prepare_embeddings(words, pos_tags)
        for word in reversed(words):
            self._buffer.append(word)
            assert word in self._word_emb
            self.buffer_lstm.push(self._word_emb[word])

    def do_action(self, action: ActionId) -> None:
        legal, message = self._is_legal(action)
        if not legal:
            raise RuntimeError(message)

        if action == self.shift_action:  # SHIFT
            assert len(self._buffer) > 0
            assert len(self.buffer_lstm) > 0
            word = self._buffer.pop()
            self.buffer_lstm.pop()
            assert word in self._word_emb
            self._stack.append(StackElement(self._word_emb[word], False))
            self.stack_lstm.push(self._word_emb[word])
        elif action in self.action2nt:  # NT(X)
            nonterm = self.action2nt[action]
            try:
                self._stack.append(StackElement(self._nt_emb[nonterm], True))
                self.stack_lstm.push(self._nt_emb[nonterm])
            except KeyError:
                raise KeyError('cannot find embedding for the nonterminal; '
                               'perhaps you forgot to call .start() beforehand?')
        else:  # REDUCE
            children_emb = []
            while len(self._stack) > 0 and not self._stack[-1].is_open_nt:
                children_emb.append(self._stack.pop().emb)
            assert len(children_emb) > 0
            assert len(self._stack) > 0
            open_nt_emb = self._stack.pop().emb
            composed_emb = self._compose(open_nt_emb, list(reversed(children_emb)))
            self._stack.append(StackElement(composed_emb, False))

        self._history.append(action)
        try:
            self.history_lstm.push(self._action_emb[action])
        except KeyError:
            raise KeyError('cannot find embedding for the action; '
                           'perhaps you forgot to call .start() beforehand?')

    def forward(self):
        lstms_emb = torch.cat([
            self.stack_lstm.top, self.buffer_lstm.top, self.history_lstm.top
        ]).view(1, -1)
        parser_summary = self.lstms2summary(lstms_emb)
        return log_softmax(self.summary2actions(parser_summary),
                           restrictions=self._get_illegal_actions()).view(-1)

    def _prepare_embeddings(self, words: Collection[WordId], pos_tags: Collection[POSId]):
        assert len(words) == len(pos_tags)
        nonterms = list(self.action2nt.values())
        actions = range(self.num_actions)

        word_indices = Variable(self._new(words).long().view(1, -1))
        pos_indices = Variable(self._new(pos_tags).long().view(1, -1))
        nt_indices = Variable(self._new(nonterms).long().view(1, -1))
        action_indices = Variable(self._new(actions).long().view(1, -1))

        word_embs = self.word_emb(word_indices).view(-1, self.word_dim)
        pos_embs = self.pos_emb(pos_indices).view(-1, self.pos_dim)
        nt_embs = self.nt_emb(nt_indices).view(-1, self.nt_dim)
        action_embs = self.action_emb(action_indices).view(-1, self.action_dim)

        final_word_embs = self.word2lstm(torch.cat([word_embs, pos_embs], dim=1))
        final_nt_embs = self.nt2lstm(nt_embs)
        final_action_embs = self.action2lstm(action_embs)

        self._word_emb = dict(zip(words, final_word_embs))
        self._nt_emb = dict(zip(nonterms, final_nt_embs))
        self._action_emb = dict(zip(actions, final_action_embs))

    def _compose(self, open_nt_emb: Variable, children_emb: Sequence[Variable]) -> Variable:
        assert open_nt_emb.dim() == 1
        assert all(x.dim() == 1 for x in children_emb)
        assert open_nt_emb.size(0) == self.input_dim
        assert all(x.size(0) == self.input_dim for x in children_emb)

        fwd_input = [open_nt_emb]
        fwd_input.extend(children_emb)
        bwd_input = [open_nt_emb]
        bwd_input.extend(reversed(children_emb))

        fwd_input = torch.cat(fwd_input).view(-1, 1, self.input_dim)
        bwd_input = torch.cat(bwd_input).view(-1, 1, self.input_dim)
        fwd_output, _ = self.compose_fwd_lstm(fwd_input, self._init_compose_states())
        bwd_output, _ = self.compose_bwd_lstm(bwd_input, self._init_compose_states())
        fwd_emb = F.dropout(fwd_output[-1, 0], p=self.dropout, training=self.training)
        bwd_emb = F.dropout(bwd_output[-1, 0], p=self.dropout, training=self.training)
        return self.compose2final(torch.cat([fwd_emb, bwd_emb]).view(1, -1)).view(-1)

    def _get_illegal_actions(self):
        illegal_actions = [action for action in range(self.num_actions)
                           if not self._is_legal(action)[0]]
        return self._new(illegal_actions).long()

    def _is_legal(self, action: ActionId) -> Tuple[bool, str]:
        nt_actions = self.action2nt.keys()
        n = self.num_open_nt
        if action in nt_actions:  # NT(X)
            if len(self._buffer) == 0:
                return False, 'cannot do NT(X) when input buffer is empty'
            elif n >= self.MAX_OPEN_NT:
                return False, 'max number of open nonterminals is reached'
            else:
                return True, ''
        elif action == self.shift_action:  # SHIFT
            if len(self._buffer) == 0:
                return False, 'cannot SHIFT when input buffer is empty'
            elif n == 0:
                return False, 'cannot SHIFT when no open nonterminal exists'
            else:
                return True, ''
        else:  # REDUCE
            last_is_nt = len(self._history) > 0 and self._history[-1] in nt_actions
            if last_is_nt:
                return False, 'cannot REDUCE when top of stack is an open nonterminal'
            elif n < 2 and len(self._buffer) > 0:
                return False, 'cannot REDUCE because there are words not SHIFT-ed yet'
            else:
                return True, ''

    def reset_parameters(self) -> None:
        # Stack LSTMs
        for name in ['stack', 'buffer', 'history']:
            lstm = getattr(self, f'{name}_lstm')
            for pname, pval in lstm.named_parameters():
                if pname.startswith('lstm.weight'):
                    init.orthogonal(pval)
                else:
                    assert pname.startswith('lstm.bias') or pname in ('h0', 'c0')
                    init.constant(pval, 0.)

        # Composition
        for name in ['fwd', 'bwd']:
            lstm = getattr(self, f'compose_{name}_lstm')
            for pname, pval in lstm.named_parameters():
                if pname.startswith('weight'):
                    init.orthogonal(pval)
                else:
                    assert pname.startswith('bias')
                    init.constant(pval, 0.)

        # Transformations
        for name in ['word', 'nt', 'action']:
            layer = getattr(self, f'{name}2lstm')
            init.xavier_uniform(layer.linear.weight, gain=init.calculate_gain('relu'))
            init.constant(layer.linear.bias, 1.)
        init.xavier_uniform(self.compose2final.linear.weight, gain=init.calculate_gain('relu'))
        init.constant(self.compose2final.linear.bias, 1.)
        init.xavier_uniform(self.lstms2summary.linear.weight, gain=init.calculate_gain('relu'))
        init.constant(self.lstms2summary.linear.bias, 1.)
        init.xavier_uniform(self.summary2actions.weight)
        init.constant(self.summary2actions.bias, 0.)

        # Embeddings
        for name in ['word', 'pos', 'nt', 'action']:
            layer = getattr(self, f'{name}_emb')
            init.uniform(layer.weight, -0.01, 0.01)

        # Guards
        for name in ['stack', 'buffer', 'history']:
            guard = getattr(self, f'{name}_guard')
            init.uniform(guard, -0.01, 0.01)

    def _init_compose_states(self) -> Tuple[Variable, Variable]:
        h0 = Variable(self._new(self.num_layers, 1, self.input_dim).zero_())
        c0 = Variable(self._new(self.num_layers, 1, self.input_dim).zero_())
        return (h0, c0)

    def _new(self, *args, **kwargs):
        return next(self.parameters()).data.new(*args, **kwargs)
