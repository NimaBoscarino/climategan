"""Complete Generator architecture:
    * OmniGenerator
    * Encoder
    * Decoders
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import softmax

import omnigan.strings as strings
from omnigan.blocks import (
    BaseDecoder,
    Conv2dBlock,
    DADADepthRegressionDecoder,
    InterpolateNearest2d,
    PainterSpadeDecoder,
    SPADEResnetBlock,
)
from omnigan.deeplab import (
    DeepLabV2Decoder,
    DeeplabV2Encoder,
    DeepLabV3Decoder,
    build_v3_backbone,
)
from omnigan.encoder import BaseEncoder
from omnigan.tutils import init_weights, normalize, mix_noise
from omnigan.utils import find_target_size

from pathlib import Path
import yaml
from addict import Dict


def get_gen(opts, latent_shape=None, verbose=0, no_init=False):
    G = OmniGenerator(opts, latent_shape, verbose, no_init)
    if no_init:
        return G

    for model in G.decoders:
        net = G.decoders[model]
        if model == "s":
            continue
        if isinstance(net, nn.ModuleDict):
            for domain, domain_model in net.items():
                init_weights(
                    net[domain_model],
                    init_type=opts.gen[model].init_type,
                    init_gain=opts.gen[model].init_gain,
                    verbose=verbose,
                    caller=f"get_gen decoder {model} {domain}",
                )
        else:
            init_weights(
                G.decoders[model],
                init_type=opts.gen[model].init_type,
                init_gain=opts.gen[model].init_gain,
                verbose=verbose,
                caller=f"get_gen decoder {model}",
            )
    if G.encoder is not None and opts.gen.encoder.architecture == "base":
        init_weights(
            G.encoder,
            init_type=opts.gen.encoder.init_type,
            init_gain=opts.gen.encoder.init_gain,
            verbose=verbose,
            caller=f"get_gen encoder",
        )
    return G


class OmniGenerator(nn.Module):
    def __init__(self, opts, latent_shape=None, verbose=0, no_init=False):
        """Creates the generator. All decoders listed in opts.gen will be added
        to the Generator.decoders ModuleDict if opts.gen.DecoderInitial is not True.
        Then can be accessed as G.decoders.T or G.decoders["T"] for instance,
        for the image Translation decoder

        Args:
            opts (addict.Dict): configuration dict
        """
        super().__init__()
        self.opts = opts
        self.verbose = verbose
        self.encoder = None
        if any(t in opts.tasks for t in "msd"):
            if opts.gen.encoder.architecture == "deeplabv2":
                if self.verbose > 0:
                    print("  - Add Deeplabv2 Encoder")
                self.encoder = DeeplabV2Encoder(opts, no_init, verbose)
            elif opts.gen.encoder.architecture == "deeplabv3":
                if self.verbose > 0:
                    backone = opts.gen.deeplabv3.backbone
                    print("  - Add Deeplabv3 ({}) Encoder".format(backone))
                self.encoder = build_v3_backbone(opts, no_init)
            else:
                self.encoder = BaseEncoder(opts)
                if self.verbose > 0:
                    print("  - Add Base Encoder")

        self.decoders = {}

        if "d" in opts.tasks:
            if opts.gen.d.architecture == "base":
                self.decoders["d"] = BaseDepthDecoder(opts)
            else:
                self.decoders["d"] = DADADepthRegressionDecoder(opts)

            if self.verbose > 0:
                print(f"  - Add {self.decoders['d'].__class__.__name__}")

        if "s" in opts.tasks:
            if opts.gen.s.architecture == "deeplabv2":
                self.decoders["s"] = DeepLabV2Decoder(opts)
                if self.verbose > 0:
                    print("  - Add DeepLabV2Decoder")
            elif opts.gen.s.architecture == "deeplabv3":
                self.decoders["s"] = DeepLabV3Decoder(opts, no_init)
                if self.verbose > 0:
                    print("  - Add DeepLabV3Decoder")
            else:
                raise NotImplementedError(
                    "Unknown architecture {}".format(opts.gen.s.architecture)
                )

        if "m" in opts.tasks:
            if self.verbose > 0:
                print("  - Add Mask Decoder")
            if self.opts.gen.m.use_spade:
                assert "d" in self.opts.tasks or "s" in self.opts.tasks
                self.decoders["m"] = MaskSpadeDecoder(opts)
            else:
                self.decoders["m"] = MaskBaseDecoder(opts)

        self.decoders = nn.ModuleDict(self.decoders)

        if "p" in self.opts.tasks:
            self.painter = PainterSpadeDecoder(opts)
            if self.verbose > 0:
                print("  - Add PainterSpadeDecoder Painter")
        else:
            self.painter = nn.Module()
            if self.verbose > 0:
                print("  - Add Empty Painter")

    def encode(self, x):
        assert self.encoder is not None
        return self.encoder.forward(x)

    def __str__(self):
        return strings.generator(self)

    def sample_painter_z(self, batch_size, device, force_half=False):
        if self.opts.gen.p.no_z:
            return None

        z = torch.empty(
            batch_size,
            self.opts.gen.p.latent_dim,
            self.painter.z_h,
            self.painter.z_w,
            device=device,
        ).normal_(mean=0, std=1.0)

        if force_half:
            z = z.half()

        return z

    def make_m_cond(self, d, s, x):
        if self.opts.gen.m.spade.detach:
            d = d.detach()
            s = s.detach()
        cats = [normalize(d), softmax(s, dim=1)]
        if self.opts.gen.m.spade.cond_nc == 15:
            assert x is not None
            cats += [
                F.interpolate(x, s.shape[-2:], mode="bilinear", align_corners=True)
            ]

        return torch.cat(cats, dim=1)

    def mask(self, x=None, z=None, cond=None, sigmoid=True):
        assert x is not None or z is not None
        assert not (x is not None and z is not None)
        if z is None:
            z = self.encode(x)

        if cond is None and self.opts.gen.m.use_spade:
            assert "s" in self.opts.tasks and "d" in self.opts.tasks
            with torch.no_grad():
                d_pred, z_depth = self.decoders["d"](z)
                s_pred = self.decoders["s"](z, z_depth)
                cond = self.make_m_cond(d_pred, s_pred, x)

        if cond is not None:
            device = z[0].device if isinstance(z, (tuple, list)) else z.device
            cond = cond.to(device)

        logits = self.decoders["m"](z, cond)

        if not sigmoid:
            return logits

        return torch.sigmoid(logits)

    def paint(self, m, x, no_paste=False):
        """
        Paints given a mask and an image
        calls painter(z, x * (1.0 - m))
        Mask has 1s where water should be painted

        Args:
            m (torch.Tensor): Mask
            x (torch.Tensor): Image to paint

        Returns:
            torch.Tensor: painted image
        """
        z_paint = self.sample_painter_z(x.shape[0], x.device)
        m = m.to(x.dtype)
        fake = self.painter(z_paint, x * (1.0 - m))
        if self.opts.gen.p.paste_original_content and not no_paste:
            return x * (1.0 - m) + fake * m
        return fake

    def paint_cloudy(self, m, x, s, sky_idx=9, res=(8, 8), weight=0.8):
        sky_mask = (
            torch.argmax(
                F.interpolate(s, x.shape[-2:], mode="bilinear"), dim=1, keepdim=True
            )
            == sky_idx
        ).to(x.dtype)
        noised_x = mix_noise(x, sky_mask, res=res, weight=weight).to(x.dtype)
        fake = self.paint(m, noised_x, no_paste=True)
        return x * (1.0 - m) + fake * m

    def depth_image(self, x=None, z=None):
        assert x is not None or z is not None
        assert not (x is not None and z is not None)
        if z is None:
            z = self.encode(x)
        logits = self.decoders["d"](z)

        if logits.shape[1] > 1:
            logits = torch.argmax(logits, dim=1)
            logits = logits / logits.max()

        return logits

    def load_val_painter(self):
        try:
            assert self.opts.val.val_painter
            ckpt_path = Path(self.opts.val.val_painter).resolve()
            assert ckpt_path.exists()
            opts_path = ckpt_path.parent.parent / "opts.yaml"
            assert opts_path.exists()
            with opts_path.open("r") as f:
                val_painter_opts = Dict(yaml.safe_load(f))
            state_dict = torch.load(ckpt_path)
            painter = PainterSpadeDecoder(val_painter_opts)
            painter.load_state_dict(
                {k.replace("painter.", ""): v for k, v in state_dict["G"].items()}
            )
            self.painter = painter
            print("    - Loaded validation-only painter")
            return True
        except Exception as e:
            print(e)
            print(">>> WARNINT: error (^) in load_val_painter, aborting.")
            return False


class MaskBaseDecoder(BaseDecoder):
    def __init__(self, opts):
        low_level_feats_dim = -1
        use_v3 = opts.gen.encoder.architecture == "deeplabv3"
        use_mobile_net = opts.gen.deeplabv3.backbone == "mobilenet"
        use_low = opts.gen.m.use_low_level_feats

        if use_v3 and use_mobile_net:
            input_dim = 320
            if use_low:
                low_level_feats_dim = 24
        elif use_v3:
            input_dim = 2048
            if use_low:
                low_level_feats_dim = 256
        else:
            input_dim = 2048

        super().__init__(
            n_upsample=opts.gen.m.n_upsample,
            n_res=opts.gen.m.n_res,
            input_dim=input_dim,
            proj_dim=opts.gen.m.proj_dim,
            output_dim=opts.gen.m.output_dim,
            norm=opts.gen.m.norm,
            activ=opts.gen.m.activ,
            pad_type=opts.gen.m.pad_type,
            output_activ="none",
            low_level_feats_dim=low_level_feats_dim,
        )


class BaseDepthDecoder(BaseDecoder):
    def __init__(self, opts):
        low_level_feats_dim = -1
        use_v3 = opts.gen.encoder.architecture == "deeplabv3"
        use_mobile_net = opts.gen.deeplabv3.backbone == "mobilenet"
        use_low = opts.gen.d.use_low_level_feats

        if use_v3 and use_mobile_net:
            input_dim = 320
            if use_low:
                low_level_feats_dim = 24
        elif use_v3:
            input_dim = 2048
            if use_low:
                low_level_feats_dim = 256
        else:
            input_dim = 2048

        n_upsample = 1 if opts.gen.d.upsample_featuremaps else 0
        output_dim = (
            1
            if not opts.gen.d.classify.enable
            else opts.gen.d.classify.linspace.buckets
        )

        self._target_size = find_target_size(opts, "d")
        print(
            "      - {}:  setting target size to {}".format(
                self.__class__.__name__, self._target_size
            )
        )

        super().__init__(
            n_upsample=n_upsample,
            n_res=opts.gen.d.n_res,
            input_dim=input_dim,
            proj_dim=opts.gen.d.proj_dim,
            output_dim=output_dim,
            norm=opts.gen.d.norm,
            activ=opts.gen.d.activ,
            pad_type=opts.gen.d.pad_type,
            output_activ="none",
            low_level_feats_dim=low_level_feats_dim,
        )

    def set_target_size(self, size):
        """
        Set final interpolation's target size

        Args:
            size (int, list, tuple): target size (h, w). If int, target will be (i, i)
        """
        if isinstance(size, (list, tuple)):
            self._target_size = size[:2]
        else:
            self._target_size = (size, size)

    def forward(self, z, cond=None):
        if self._target_size is None:
            error = "self._target_size should be set with self.set_target_size()"
            error += "to interpolate depth to the target depth map's size"
            raise ValueError(error)

        d = super().forward(z)

        preds = F.interpolate(
            d, size=self._target_size, mode="bilinear", align_corners=True
        )

        return preds, None


class MaskSpadeDecoder(nn.Module):
    def __init__(self, opts):
        """Create a SPADE-based decoder, which forwards z and the conditioning
        tensors seg (in the original paper, conditioning is on a semantic map only).
        All along, z is conditioned on seg. First 3 SpadeResblocks (SRB) do not shrink
        the channel dimension, and an upsampling is applied after each. Therefore
        2 upsamplings at this point. Then, for each remaining upsamplings
        (w.r.t. spade_n_up), the SRB shrinks channels by 2. Before final conv to get 3
        channels, the number of channels is therefore:
            final_nc = channels(z) * 2 ** (spade_n_up - 2)
        Args:
            latent_dim (tuple): z's shape (only the number of channels matters)
            cond_nc (int): conditioning tensor's expected number of channels
            spade_n_up (int): Number of total upsamplings from z
            spade_use_spectral_norm (bool): use spectral normalization?
            spade_param_free_norm (str): norm to use before SPADE de-normalization
            spade_kernel_size (int): SPADE conv layers' kernel size
        Returns:
            [type]: [description]
        """
        super().__init__()
        self.opts = opts
        latent_dim = opts.gen.m.spade.latent_dim
        cond_nc = opts.gen.m.spade.cond_nc
        spade_use_spectral_norm = opts.gen.m.spade.spade_use_spectral_norm
        spade_param_free_norm = opts.gen.m.spade.spade_param_free_norm
        spade_kernel_size = 3
        self.num_layers = opts.gen.m.spade.num_layers
        self.z_nc = latent_dim

        if (
            opts.gen.encoder.architecture == "deeplabv3"
            and opts.gen.deeplabv3.backbone == "mobilenet"
        ):
            self.input_dim = [320, 24]
            self.low_level_conv = Conv2dBlock(
                self.input_dim[1],
                self.input_dim[0],
                3,
                padding=1,
                activation="lrelu",
                pad_type="reflect",
                norm="spectral_batch",
            )
            self.merge_feats_conv = Conv2dBlock(
                self.input_dim[0] * 2,
                self.z_nc,
                3,
                padding=1,
                activation="lrelu",
                pad_type="reflect",
                norm="spectral_batch",
            )
        elif (
            opts.gen.encoder.architecture == "deeplabv3"
            and opts.gen.deeplabv3.backbone == "resnet"
        ):
            self.input_dim = [2048, 256]
            self.low_level_conv = Conv2dBlock(
                self.input_dim[1],
                self.input_dim[0],
                3,
                padding=1,
                activation="lrelu",
                pad_type="reflect",
                norm="spectral_batch",
            )
            self.merge_feats_conv = Conv2dBlock(
                self.input_dim[0] * 2,
                self.z_nc,
                3,
                padding=1,
                activation="lrelu",
                pad_type="reflect",
                norm="spectral_batch",
            )

        elif opts.gen.encoder.architecture == "deeplabv2":
            self.input_dim = 2048
            self.fc_conv = Conv2dBlock(
                self.input_dim,
                self.z_nc,
                3,
                padding=1,
                activation="lrelu",
                pad_type="reflect",
                norm="spectral_batch",
            )
        else:
            raise ValueError("Unknown encoder type")

        self.spade_blocks = []
        for i in range(self.num_layers):
            self.spade_blocks.append(
                SPADEResnetBlock(
                    int(self.z_nc / (2 ** i)),
                    int(self.z_nc / (2 ** (i + 1))),
                    cond_nc,
                    spade_use_spectral_norm,
                    spade_param_free_norm,
                    spade_kernel_size,
                ).cuda()
            )
        self.spade_blocks = nn.Sequential(*self.spade_blocks)

        self.final_nc = int(self.z_nc / (2 ** self.num_layers))
        self.mask_conv = Conv2dBlock(
            self.final_nc, 1, 3, padding=1, norm="none", activation="none",
        )
        self.upsample = InterpolateNearest2d(scale_factor=2)

    def forward(self, z, cond):
        if isinstance(z, (list, tuple)):
            z_h, z_l = z
            z_l = self.low_level_conv(z_l)
            z_l = F.interpolate(z_l, size=z_h.shape[-2:], mode="bilinear")
            z = torch.cat([z_h, z_l], axis=1)
            y = self.merge_feats_conv(z)
        else:
            y = self.fc_conv(z)

        for i in range(self.num_layers):
            y = self.spade_blocks[i](y, cond)
            y = self.upsample(y)
        y = self.mask_conv(y)
        return y

    def __str__(self):
        return "MaskerSpadeDecoder"
        # return strings.spadedecoder(self)
