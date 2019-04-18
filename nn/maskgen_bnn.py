import numpy as np
import torch
import torch.nn.functional as F
from models.model_helpers import ParamsFlattener
from nn.relaxed_bb import RelaxedBetaBernoulli
from torch import nn

try:
  from utils import utils
except ModuleNotFoundError:
  # For execution as a __main__ file
  import sys
  sys.path.append('../')
  from utils import utils


C = utils.getCudaManager('default')


class FeatureGenerator(nn.Module):
  """This model will generate coordinate-wise features considering taking
  their uncertainty into account. MCdropout aids model to sample mutiple number
  of most promising update behavior without additive cost.Each coordinate will
  be passed as a set of intances along the batch dimension.
    Inputs: grad, weight, grad with k decaying weights
    Outputs: feature for each coordinates
  """

  def __init__(self, hidden_sz=32, n_layers=1, batch_std_norm=True,
               decay_values=[0.5, 0.9, 0.99, 0.999, 0.9999], drop_rate=0.5):
    super().__init__()
    assert n_layers > 0  # has to have at least single layer
    assert isinstance(decay_values, (list, tuple))

    self.input_sz = 2 + len(decay_values)  # grad, weight, momentums
    self.hidden_sz = hidden_sz
    self.batch_std_norm = batch_std_norm
    self.decay_values = C(torch.tensor(decay_values))
    self.momentum = None
    self.momentum_sq = None
    self.drop_rate = drop_rate
    self.layers = nn.ModuleList()
    for i in range(n_layers):
      input_sz = self.input_sz if i == 0 else hidden_sz
      self.layers.append(nn.Sequential(
        nn.Linear(input_sz, hidden_sz),
        nn.ReLU(inplace=True),
      ))

  def new(self):
    # reset momentum
    self.momentum = None
    return self

  def forward(self, g, w, p=None):
    """
    Args:
      g(tensor): current gradient (n_params x 1)
      w(tensor): current weight (n_params x 1)
      p(float): for setting dropout probability in runtime.
        By default, use self.drop_rate.

    Returns:
      x(tensor): representation for step & mask generation
                 (n_params x hidden_sz)
    """
    if self.momentum is None:
      # Initialize momentum matrix
      #   momentum matrix: m[B, len(decay_values)]
      #   should keep track of momentums for each B dimension
      self.momentum = g.repeat(1, len(self.decay_values))
      self.momentum_sq = (g**2).repeat(1, len(self.decay_values))
    else:
      # Dimension will be matched by broadcasting
      r = self.decay_values
      self.momentum = r * self.momentum + (1 - r) * g
      self.momentum_sq = r * self.momentum_sq + (1 - r) * (g**2)

    # # regularization(as in Metz et al.)
    # g_sq = g**2
    if self.batch_std_norm:
      g = g.div(g.var(0, keepdim=True))
      # g_sq = g_sq.div(g_sq.var(0, keepdim=True))
      w = w.div(w.var(0, keepdim=True))
    #
    x = torch.cat([g, w, self.momentum], 1).detach()
    # x = torch.cat([g, g_sq], dim=1)
    # x = torch.cat([g, g_sq], dim=1)
    """Submodule inputs:
       x[n_params, 0]: current gradient
       x[n_params, 1]: current weight
       x[n_params, 2:]: momentums (for each decaying values)
    """
    assert x.size(1) == self.input_sz
    p = self.drop_rate if p is None else p
    for layer in self.layers:
      x = layer(x)
      # Testime MC dropout
      x = F.dropout(x, p)
    assert x.size(1) == self.hidden_sz

    return x  # coordinate-wise feature


class StepGenerator(nn.Module):
  def __init__(self, hidden_sz=32, n_layers=1, out_temp=1e-5):
    super().__init__()
    assert n_layers > 0  # has to have at least single layer
    self.hidden_sz = hidden_sz
    self.output_sz = 2  # log learning rate, update direction
    self.out_temp = out_temp
    self.layers = nn.ModuleList()
    for i in range(n_layers):
      output_sz = self.output_sz if (i == n_layers - 1) else hidden_sz
      self.layers.append(nn.Linear(hidden_sz, output_sz))
      if i < n_layers - 1:
        self.layers.append(nn.Tanh())
        # NOTE: last nonliearity can be critical (avoid ReLU + exp > 1)

  def forward(self, x, debug=False):
    """
    Args:
      x (tensor): representation for step & mask generation
                  (n_params x hidden_sz)

    Returns:
      step (tensor): update vector in the parameter space. (n_params x 1)
    """
    assert x.size(1) == self.hidden_sz

    for layer in self.layers:
      x = layer(x)
    """Submodule outputs:
      y[n_params, 0]: per parameter log learning rate
      y[n_params, 1]: unnormalized update direction
    """
    assert x.size(1) == self.output_sz
    out_1 = x[:, 0]  # * self.out_temp
    out_2 = x[:, 1]  # * self.out_temp # NOTE: normalizing?
    # out_3 = x[:, 2]
    # out_3 = out_3.div(out_3.norm(p=2).detach())
    step = torch.exp(out_1 * self.out_temp) * out_2 * self.out_temp
    if debug:
      import pdb
      pdb.set_trace()
    # import pdb; pdb.set_trace()
    step = torch.clamp(step, max=0.01, min=-0.01)
    return step

  def forward_with_mask(self, x):
    pass


class MaskGenerator(nn.Module):
  def __init__(self, hidden_sz=512, n_layers=1):
    super().__init__()
    assert n_layers > 0
    self.hidden_sz = hidden_sz
    self.gamm_g = None
    self.gamm_l = None

    self.nonlinear = nn.Tanh()
    self.eyes = None
    self.ones = None
    self.zeros = None
    self.avg_layers = nn.ModuleList()
    for i in range(n_layers):
      output_sz = 3 if (i == n_layers - 1) else hidden_sz
      # a, b, lamb, gamm_g, gamm_l
      self.avg_layers.append(nn.Linear(hidden_sz, output_sz))
    self.out_layers = nn.ModuleList()
    for i in range(n_layers):
      output_sz = 2 if (i == n_layers - 1) else hidden_sz
      # a, b, lamb, gamm_g, gamm_l
      self.out_layers.append(nn.Linear(hidden_sz, output_sz))
    self.beta_bern = RelaxedBetaBernoulli()
    self.sigmoid = nn.Sigmoid()
    self.sigmoid_temp = 1e1
    self.gamm_scale = 1e-3
    self.lamb_scale = 1
    self.p_lamb = nn.Parameter(torch.ones(1))
    self.p_gamm = nn.Parameter(torch.ones(1))

  def detach_lambdas_(self):
    if self.gamm_g is not None:
      self.gamm_g = self.gamm_g.detach()
    if self.gamm_l is not None:
      self.gamm_l = [l.detach() for l in self.gamm_l]
    if self.lamb is not None:
      self.lamb = self.lamb.detach()

  def build_block_diag(self, tensors):
    """Build block diagonal matrix by aligning input tensors along the digonal
    elements and filling the zero blocks for the rest of area.

    Args: list (list of square matrices(torch.tensor))

    Returns: tensor (block diagonal matrix)
    """
    # left and right marginal zero matrices
    #   (to avoid duplicate allocation onto GPU)
    if self.zeros is None:
      blocks = []
      offset = 0
      size = [s.size(0) for s in tensors]
      total_size = sum(size)
      self.zeros = []
      # NOTE: exception handling for different tnesors size
      for i in range(len(tensors)):
        blocks_sub = []
        cur = tensors[i].size(0)
        if i == 0:
          blocks_sub.append(None)
        else:
          blocks_sub.append(C(torch.zeros(cur, offset)))
        offset += tensors[i].size(0)
        if i == len(tensors) - 1:
          blocks_sub.append(None)
        else:
          blocks_sub.append(C(torch.zeros(cur, total_size - offset)))
        self.zeros.append(blocks_sub)
    # concatenate the tensors with left and right block zero matices
    blocks = []
    for i in range(len(tensors)):
      blocks_sub = []
      zeros = self.zeros[i]
      if zeros[0] is not None:
        blocks_sub.append(zeros[0])
      blocks_sub.append(tensors[i])
      if zeros[1] is not None:
        blocks_sub.append(zeros[1])
      blocks.append(torch.cat(blocks_sub, dim=1))
    return torch.cat(blocks, dim=0)

  def forward(self, x, size, debug=False):
    """
    Args:
      x (tensor): representation for step & mask generation (batch x hidden_sz)
      size (dict): A dict mapping names of weight matrices to their
        torch.Size(). For example:

      {'mat_0': torch.Size([784, 500]), 'bias_0': torch.Size([500]),
       'mat_1': torch.Size([500, 10]), 'bias_1': torch.Size([10])}
    """
    assert isinstance(size, dict)
    assert x.size(1) == self.hidden_sz

    """Set-based feature averaging (Permutation invariant)"""
    sizes = [s for s in size.values()]
    # [torch.Size([784, 500]), torch.Size([500]),
    #  torch.Size([500, 10]), torch.Size([10])]
    split_sizes = [np.prod(s) for s in size.values()]
    # [392000, 500, 5000, 10]
    # split_size = [sum(offsets[:i]) for i in range(len(offsets))]
    x_set = torch.split(x, split_sizes, dim=0)
    # mean = torch.cat([s.mean(0).expand_as(s) for s in sections], dim=0)
    x_set = [x.view(*s, self.hidden_sz) for x, s in zip(x_set, sizes)]
    x_set_sizes = [x_set[i].size() for i in range(len(x_set))]
    # [torch.Size([784, 500, 32]), torch.Size([500, 32]),
    #  torch.Size([500, 10, 32]), torch.Size([10, 32])]

    # unsqueeze bias to match dim
    max_dim = max([len(s) for s in x_set_sizes])  # +1: hidden dimension added
    assert all([(max_dim - len(s)) <= 1 for s in x_set_sizes])
    x_set = [x.unsqueeze_(0) if len(x.size()) < max_dim else x for x in x_set]
    # [torch.Size([784, 500, 32]), torch.Size([1, 500, 32]),
    #  torch.Size([500, 10, 32]), torch.Size([1, 10, 32])]

    # concatenate bias hidden with weight hidden in the same layer
    # bias will be dropped with its corresponding weight column
    #   (a) group the parameters layerwisely
    names = [n for n in size.keys()]
    # ['mat_0', 'bias_0', 'mat_1', 'bias_1']
    layerwise = {}
    for n, x in zip(names, x_set):
      n_set = n.split('_')[1]
      if n_set in layerwise:
        layerwise[n_set].append(x)
      else:
        layerwise[n_set] = [x]
    #   (b) compute mean over the set
    x_set = [torch.cat(xs, dim=0).mean(0) for xs in layerwise.values()]
    x_set_sizes = [x_set[i].size() for i in range(len(x_set))]

    # non-linearity
    x_set = self.nonlinear(torch.cat(x_set, dim=0))
    # x_set: [total n of set(channels) x hidden_sz]

    """Reflecting relative importance (Permutation equivariant)"""
    # to avoid allocate new tensor to GPU at every iteration
    if self.eyes is None or not self.eyes.size(0) == (x_set.size(0) * 2):
      self.eyes = C(torch.eye(x_set.size(0)))
      self.ones = C(torch.ones(x_set.size(0), x_set.size(0)))
      # self.zeros = [torch.zeros(s[0]) for s in x_set_sizes]

    # Identity term w_i: [(total n of set)^2]
    w_i = self.eyes
    # Relativity term w_r: [(total n of set)^2]
    if self.gamm_g is None or self.gamm_l is None:
      # .normal_(0, 1e-3))
      ones = C(torch.ones([x_set.size(0)] * 2))
      self.gamm_g = self.gamm_l = ones  * self.gamm_scale
      self.lamb = ones * self.lamb_scale

    # gamm_gl = (self.gamm_g + self.gamm_l)
    for layer in self.avg_layers:
      out = layer(x_set)

    lamb = out[:, 0].unsqueeze(-1)
    self.lamb = lamb.mean(0) * self.lamb_scale
    lamb = self.lamb.expand_as(lamb)
    # self.lamb = lamb.div(lamb.size(0))
    lamb = lamb * self.eyes

    # out[:, 1] -> globally averaged lamba_g
    gamm_g = out[:, 1].unsqueeze(-1)
    self.gamm_g = gamm_g.mean(0) * self.gamm_scale
    gamm_g = self.gamm_g.expand_as(gamm_g)
    # self.gamm_g = gamm_g.div(gamm_g.size(0))
    gamm_g = gamm_g * self.ones

    # out[:, 2] -> locally averaged gamm_l
    gamm_l = out[:, 2].unsqueeze(-1)
    gamm_l = torch.split(gamm_l, [s[0] for s in x_set_sizes], dim=0)
    self.gamm_l = [l.mean(0) * self.gamm_scale for l in gamm_l]
    sizes = [(l.size(0),) * 2 for l in gamm_l]
    # # gamm_l = [l.mean(0).expand(l.size(0)) for l in gamm_l]
    # gamm_l = [l.div(l.size(0)).expand(l.size(0)) for l in gamm_l]
    # gamm_l = torch.cat(gamm_l, dim=0)
    gamm_l = [l.mean(0).expand(s) for l, s in zip(self.gamm_l, sizes)]
    # gamm_l = [l.div(l.size(0)).expand([l.size(0)] * 2) for l in gamm_l]
    gamm_l = self.build_block_diag(gamm_l)

    if debug:
      import pdb; pdb.set_trace()
    # Equivariant weight multiplication
    # inp = x_set.t().matmul(self.eyes + self.gamm_g + self.gamm_l)
    inp = x_set.t().matmul(lamb + gamm_g + gamm_l)
    # term_2 = x_set.t().matmul(gamm_gl * self.ones)
    # inp = term_1 + term_2
    # NOTE: make it faster?
    # last layers for mask generation
    for layer in self.out_layers:
      out = layer(inp.t())

    # out[:, 0] -> mask
    mask, pi, kld = self.beta_bern(out[:, :2])

    # mask_logit = out[:, 0]
    # mask = self.sigmoid(mask_logit * self.sigmoid_temp)
    # kld = mask.norm(p=1)
    mask = torch.split(mask, [s[0] for s in x_set_sizes], dim=0)
    name = [n for n in layerwise.keys()]
    mask = {'layer_' + n: m for n, m in zip(name, mask)}

    # # out[:, 1] -> globally averaged lamba_g
    # gamm_g = out[:, 2].unsqueeze(-1)
    # gamm_g = gamm_g.mean(0).expand_as(gamm_g)
    # # gamm_g = gamm_g.div(gamm_g.size(0))
    # self.gamm_g = gamm_g * self.ones * self.gamm_scale
    # # gamm_g = out[:, 2].unsqueeze(-1)
    # # gamm_g = torch.split(gamm_g, [s[0] for s in x_set_sizes], dim=0)
    # # gamm_g = torch.cat([l.mean(0).expand_as(l) for l in gamm_g], dim=0)
    # # self.gamm_g = gamm_g * self.gamm_scale
    #
    # # # out[:, 1] -> globally averaged lamba_g
    # # gamm_l = out[:, 3].unsqueeze(-1)
    # # # gamm_g = gamm_g.mean(0).expand_as(gamm_g)
    # # gamm_l = gamm_l.div(gamm_l.size(0))
    # # self.gamm_l = gamm_l # * self.gamm_scale
    #
    # # out[:, 2] -> locally averaged gamm_l
    # gamm_l = out[:, 3].unsqueeze(-1)
    # gamm_l = torch.split(gamm_l, [s[0] for s in x_set_sizes], dim=0)
    # # gamm_l = [l.mean(0).expand(l.size(0)) for l in gamm_l]
    # import pdb; pdb.set_trace()
    # gamm_l = [l.div(l.size(0)).expand(l.size(0)) for l in gamm_l]
    # gamm_l = torch.cat(gamm_l, dim=0)
    # # gamm_l = [l.mean(0).expand([l.size(0)] * 2) for l in gamm_l]
    # # # gamm_l = [l.div(l.size(0)).expand([l.size(0)] * 2) for l in gamm_l]
    # # gamm_l = self.build_block_diag(gamm_l)
    # self.gamm_l = gamm_l * self.gamm_scale
    # # import pdb; pdb.set_trace()
    #
    # lamb = out[:, 4].unsqueeze(-1)
    # lamb = lamb.mean(0).expand_as(lamb)
    # # lamb = lamb.div(lamb.size(0))
    # self.lamb = lamb * self.ones * self.lamb_scale

    return mask, pi, kld
