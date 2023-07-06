import torch
import torch.nn.functional as F
from torch import Tensor, nn


def window_partition(x: Tensor, window_size: int) -> Tensor:
    """Partitions (B, C, H, W) into (B, num_windows, C, `window_size`, `window_size`)"""
    B, C, H, W = x.shape
    num_windows = H * W // window_size ** 2
    return (
        x.reshape(B, C, H // window_size, window_size, W // window_size, window_size)
         .permute(0, 1, 2, 4, 3, 5)
         .reshape(B, C, num_windows, window_size, window_size)
         .permute(0, 2, 1, 3, 4)
    )

def window_unpartition(x: Tensor, H: int, W: int) -> Tensor:
    """Unpartitions (B, num_windows, C, `window_size`, `window_size`) into (B, C, H, W)"""
    B, _, C, window_size, _ = x.shape
    return (
        x.permute(0, 2, 1, 3, 4)
         .reshape(B, C, H // window_size, W // window_size, window_size, window_size)
         .permute(0, 1, 2, 4, 3, 5)
         .reshape(B, C, H, W)
    )

def spatial_flatten(x: Tensor) -> Tensor:
    """Flattens (*, C, H, W) into (*, H * W, C)"""
    return x.flatten(-2).transpose(-2, -1)
    
def spatial_unflatten(x: Tensor, H: int, W: int) -> Tensor:
    """Unflattens (*, H * W, C) into (*, C, H, W)"""
    return x.transpose(-2, -1).unflatten(-1, (H, W))

class MultiHeadSelfAttention(nn.Module):
    """
    Performs multi head self attention on the input sequence,
    can also transpose inputs before performing attention
    - Input: (B, N, L, `dim`)
    - Output: (B, N, L, `dim`)
    """

    def __init__(self, dim: int, transposed: bool, num_heads: int = 8) -> None:
        """
        Parameters:
        - `dim`: number of channels
        - `transposed`: if `True`, computes `(qT x k x vT)T` instead of `q x kT x v`
        - `num_heads`: number of attention heads
        """

        super().__init__()
        self.dim = dim
        self.transposed = transposed
        self.num_heads = num_heads
        self.qkv = nn.Linear(dim, dim * 3)
        self.out = nn.Linear(dim, dim)
    
    def forward(self, x: Tensor) -> Tensor:
        B, N, L, C = x.shape
        head_dim = C // self.num_heads

        x = self.qkv(x)
        x = (
            x.reshape(B, N, L, 3, self.num_heads, head_dim)
             .permute(3, 0, 1, 4, 2, 5)
        ) # (3, B, N, num_heads, L, head_dim)

        if self.transposed:
            x = torch.matmul(
                F.softmax(
                    torch.matmul(
                        x[0].transpose(-2, -1) / L,
                        x[1] / L
                    ),
                    -1
                ),
                x[2].transpose(-2, -1)
            ).permute(0, 1, 4, 2, 3) # (B, N, L, num_heads, head_dim)
        else: 
            x = torch.matmul(
                F.softmax(
                    torch.matmul(
                        x[0] / head_dim,
                        x[1].transpose(-2, -1) / head_dim
                    ),
                    -1
                ),
                x[2]
            ).permute(0, 1, 3, 2, 4) # (B, N, L, num_heads, head_dim)
        
        x = self.out(
            x.flatten(-2)
        )

        return x
    
class AttentionBlock(nn.Module):
    """
    Block of `MultiHeadSelfAttention` > `Linear` > `Linear`
    - Input: (B, N, `dim`)
    - Output: (B, N, `dim`)
    """

    def __init__(self, dim: int, transposed: bool, window_size: int = 8, num_heads: int = 8) -> None:
        """
        Parameters:
        - `dim`: number of channels
        - `transposed`: if `True`, computes `(qT x k x vT)T` instead of `q x kT x v`
        - `window_size`: size of square window
        - `num_heads`: number of attention heads
        """

        super().__init__()
        self.window_size = window_size
        self.transposed = transposed
        self.window_attention = MultiHeadSelfAttention(dim, transposed, num_heads)
        self.mlp = nn.Sequential(
            nn.Linear(dim, dim * 4),
            nn.GELU(),
            nn.Linear(dim * 4, dim)
        )
        self.norm1 = nn.LayerNorm(dim)
        self.norm2 = nn.LayerNorm(dim)

    def forward(self, x: Tensor) -> Tensor:
        _, _, H, W = x.shape

        if self.transposed:
            x = x.unsqueeze(1)
        else:
            x = window_partition(x, self.window_size)
        x = spatial_flatten(x)

        x = x + self.window_attention(self.norm1(x))
        x = x + self.mlp(self.norm2(x))

        if self.transposed:
            x = spatial_unflatten(x, H, W)
            x = x.squeeze(1)
        else:
            x = spatial_unflatten(x, self.window_size, self.window_size)
            x = window_unpartition(x, H, W)

        return x
    
class TransformerSR(nn.Module):
    """
    Sequence of `AttentionBlock` with pixel shuffle upsampling at the end
    - Input: (B, `dim`, H, W)
    - Output: (B, `dim`, `H * factor`, `W * factor`)
    """
    
    def __init__(
            self,
            factor: int,
            residual_groups: list,
            num_blocks: int,
            dim: int,
            window_size: int,
            num_heads: tuple | list,
            in_channels: int = 3,
            out_channels: int = 3
    ) -> None:
        """
        Parameters:
        - `factor`: upscaling factor, must be a power of 2
        - `residual_groups`: number of blocks between skip connections, supports multiple numbers
        - `num_blocks`: number of blocks
        - `dim`: number of channels of the main path through the model
        - `window_size`: attention window size
        - `num_heads`: number of attention heads
        - `in_channels`: number of input channels (default 3)
        - `out_channels`: number of output channels (default 3)
        """
        
        super().__init__()
        self.residual_groups = residual_groups
        self.in_conv = nn.Conv2d(in_channels, dim, kernel_size=3, padding=1)
        self.blocks = nn.ModuleList(
            nn.Sequential(
                AttentionBlock(dim, False, window_size, num_heads),
                AttentionBlock(dim, True, window_size, num_heads)
            ) for _ in range(num_blocks)
        )
        self.out_conv = nn.Sequential(
            nn.Conv2d(dim, (factor ** 2) * out_channels, kernel_size=3, padding=1),
            nn.PixelShuffle(factor)
        )
    
    def forward(self, x: Tensor) -> Tensor:
        residual = {k: 0 for k in self.residual_groups}
        x = x - 0.5

        x = self.in_conv(x)

        for i, block in enumerate(self.blocks):
            for step in residual.keys():
                if i % step == 0:
                    residual[step] = x

            x = block(x)
            
            for step in residual.keys():
                if i % step == step - 1:
                    x = x + residual[step]

        x = self.out_conv(x)
        return x + 0.5
    
class Classifier(nn.Module):
    """
    Groups of `AttentionBlock` with downsampling inbetween
    and global pool into logits at the end
    - Input: (B, `dim`, H, W)
    - Output: (B, `num_classes`)
    """

    def __init__(
        self,
        groups: tuple | list,
        dims: tuple | list,
        window_size: int,
        num_heads: int,
        num_classes: int,
        in_channels: int = 3
    ) -> None:
        """
        Parameters:
        - `groups`: number of blocks in each group, before each downsample
        - `dim`: number of channels of the main path through the model
        - `window_size`: attention window size
        - `num_heads`: number of attention heads
        - `in_channels`: number of input channels (default 3)
        - `out_channels`: number of output channels (default 3)
        """
        
        super().__init__()

        self.blocks = nn.ModuleList([nn.Conv2d(in_channels, dims[0], kernel_size=3, padding=1)])
        for i in range(len(groups)):
            if i > 0:
                self.blocks.append(nn.Conv2d(dims[i - 1], dims[i], kernel_size=2, stride=2))
            for _ in range(groups[i]):
                self.blocks += [
                    AttentionBlock(dims[i], False, window_size // 2 ** i, num_heads),
                    AttentionBlock(dims[i], True, window_size // 2 ** i, num_heads)
                ]

        self.blocks += [
            nn.AdaptiveAvgPool2d(1),
            nn.Flatten(),
            nn.Linear(dims[-1], num_classes)
        ]

    def forward(self, x: Tensor) -> Tensor:
        x = x - 0.5
        for block in self.blocks:
            x = block(x)
        return x