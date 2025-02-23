# Copyright 2024
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from dataclasses import fields
from typing import Any

import chex
import flax.linen as nn
import jax
import jax.nn.initializers as init
import jax.numpy as jnp
from flax import struct
from flax.linen import partitioning as nnp

from mu_transformer.dims import Dimensions
from mu_transformer.jax_impl.shard import sharding_constraint
from mu_transformer.jax_impl.sow import coord_check_l1

INFTY_APPROX = 1e30
MESH_AXES = Dimensions(X="X", Y="Y", N=None)


@struct.dataclass
class TransformerConfig:
    param_dtype: Any
    dtype: Any
    sequence_len: int
    d_model: int
    d_head: int
    ff_multiple: int
    e_norm: bool
    q_init: str
    r_init: str
    u_init: str
    qk_scale: float
    qk_norm: bool
    kv_mqa: bool
    rotary_base: int
    act_name: str
    act_square: bool
    norm_eps: float
    norm_gains: bool
    norm_gains_type: str
    proj_biases: bool
    n_layer: int
    n_vocab: int
    bos_token_id: int
    eos_token_id: int
    pad_token_id: int
    is_train: bool

    @classmethod
    def create(cls, **kwargs):
        signature = {field.name: field.type for field in fields(TransformerConfig)}
        flt = {k: v for k, v in kwargs.items() if k in signature}
        flt.update({k: jnp.dtype(v) for k, v in flt.items() if k.endswith("_dtype")})
        return cls(**flt)


class RMSNorm(nn.Module):
    hps: TransformerConfig
    global_mesh: jax.sharding.Mesh
    suffix: str

    @nn.compact
    def __call__(self, x):
        eps = jnp.array([self.hps.norm_eps], dtype=x.dtype)
        ms = jnp.mean(jnp.square(x), axis=-1)
        rms = jnp.sqrt(ms + eps)
        normed = x / rms[..., None]
        if self.hps.norm_gains:
            g_is_scalar = self.hps.norm_gains_type == "scalar"
            g_shape = [1] if g_is_scalar else [self.hps.d_model]
            g_mesh = MESH_AXES["N"] if g_is_scalar else MESH_AXES["Y"]
            normed *= self.param(
                "g_" + self.suffix,
                nn.with_partitioning(init.ones, g_mesh, self.global_mesh),
                g_shape,
                self.hps.param_dtype,
            ).astype(self.hps.dtype)[None, None, ...]
        return normed


class RotaryEncoding(nn.Module):
    hps: TransformerConfig
    global_mesh: jax.sharding.Mesh
    is_keys: bool

    @nn.compact
    def __call__(self, x):
        *_, length, width = x.shape

        positions = jnp.arange(length)
        positions = sharding_constraint(positions, MESH_AXES["N"], self.global_mesh)
        positions = positions[..., None]  # expand along width axis
        positions = sharding_constraint(positions, MESH_AXES["NN"], self.global_mesh)

        dimensions = jnp.arange(width // 2)  # half each for sin and cos
        dimensions = sharding_constraint(dimensions, MESH_AXES["N"], self.global_mesh)
        ang_freqs = jnp.power(self.hps.rotary_base, -dimensions / (width // 2))
        ang_freqs = ang_freqs[None, ...]  # expand along length axis
        ang_freqs = sharding_constraint(ang_freqs, MESH_AXES["NN"], self.global_mesh)

        # expand along leading axes, such as batch and head.
        positions = positions[None, None, ...]
        ang_freqs = ang_freqs[None, None, ...]
        positions = sharding_constraint(positions, MESH_AXES["NNNN"], self.global_mesh)
        ang_freqs = sharding_constraint(ang_freqs, MESH_AXES["NNNN"], self.global_mesh)
        chex.assert_shape(positions, [1, 1, length, 1])
        chex.assert_shape(ang_freqs, [1, 1, 1, width // 2])

        radians = positions * ang_freqs
        radians = sharding_constraint(radians, MESH_AXES["NNNN"], self.global_mesh)
        chex.assert_shape(radians, [1, 1, length, width // 2])

        cos = jnp.cos(radians).astype(x.dtype)
        sin = jnp.sin(radians).astype(x.dtype)
        cos = sharding_constraint(cos, MESH_AXES["NNNN"], self.global_mesh)
        sin = sharding_constraint(sin, MESH_AXES["NNNN"], self.global_mesh)
        chex.assert_shape(cos, [1, 1, length, width // 2])
        chex.assert_shape(sin, [1, 1, length, width // 2])

        broadcast = self.hps.kv_mqa and self.is_keys
        mesh_axes = MESH_AXES["XNNN"] if broadcast else MESH_AXES["XYNN"]

        even, odd = jnp.split(x, 2, axis=-1)
        even = sharding_constraint(even, mesh_axes, self.global_mesh)
        odd = sharding_constraint(odd, mesh_axes, self.global_mesh)

        r_even = even * cos - odd * sin
        r_odd = even * sin + odd * cos
        r_even = sharding_constraint(r_even, mesh_axes, self.global_mesh)
        r_odd = sharding_constraint(r_odd, mesh_axes, self.global_mesh)

        r = jnp.concatenate([r_even, r_odd], axis=-1)
        r = sharding_constraint(r, mesh_axes, self.global_mesh)
        chex.assert_shape(r, x.shape)
        return r


class CausalMask(nn.Module):
    length: int
    global_mesh: jax.sharding.Mesh

    @nn.compact
    def __call__(self, x):
        i = jnp.arange(self.length)[..., None]
        j = jnp.arange(self.length)[None, ...]
        i = sharding_constraint(i, MESH_AXES["NN"], self.global_mesh)
        j = sharding_constraint(j, MESH_AXES["NN"], self.global_mesh)
        mask = jnp.less(i, j)  # i.e., j > i, indicator masks out non-causal connections
        mask = sharding_constraint(mask, MESH_AXES["NN"], self.global_mesh)
        mask = mask[None, None, ...]
        mask = sharding_constraint(mask, MESH_AXES["NNNN"], self.global_mesh)
        x = x - jnp.array([INFTY_APPROX], dtype=x.dtype) * mask
        x = sharding_constraint(x, MESH_AXES["XYNN"], self.global_mesh)
        return x


class MultiHeadAttention(nn.Module):
    hps: TransformerConfig
    global_mesh: jax.sharding.Mesh

    @nn.compact
    def __call__(self, x):
        shapes = Dimensions(
            B=x.shape[0],
            T=self.hps.sequence_len,
            M=self.hps.d_model,
            D=self.hps.d_head,
            H=self.hps.d_model // self.hps.d_head,
            I=1,
        )
        x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)
        self.sow("intermediates", "ax_l1", coord_check_l1(x))

        stddev = self.hps.d_model**-0.5
        q_init = {"zero": init.zeros, "vs": init.normal(stddev)}[self.hps.q_init]
        kv_init = init.normal(stddev)
        o_init = {"zero": init.zeros, "vs": init.normal(stddev)}[self.hps.r_init]
        b_init = init.zeros

        w_kv_mesh_axes = MESH_AXES["XNN"] if self.hps.kv_mqa else MESH_AXES["XYN"]
        w_kv_shape = shapes["MID"] if self.hps.kv_mqa else shapes["MHD"]
        wq = self.param(
            "w_aq",
            nn.with_partitioning(q_init, MESH_AXES["XYN"], self.global_mesh),
            shapes["MHD"],
            self.hps.param_dtype,
        )
        wk = self.param(
            "w_ak",
            nn.with_partitioning(kv_init, w_kv_mesh_axes, self.global_mesh),
            w_kv_shape,
            self.hps.param_dtype,
        )
        wv = self.param(
            "w_av",
            nn.with_partitioning(kv_init, w_kv_mesh_axes, self.global_mesh),
            w_kv_shape,
            self.hps.param_dtype,
        )
        wo = self.param(
            "w_ao",
            nn.with_partitioning(o_init, MESH_AXES["YNX"], self.global_mesh),
            shapes["HDM"],
            self.hps.param_dtype,
        )
        if self.hps.proj_biases:
            b_kv_mesh_axes = MESH_AXES["NN"] if self.hps.kv_mqa else MESH_AXES["YN"]
            b_kv_shape = shapes["ID"] if self.hps.kv_mqa else shapes["HD"]
            bq = self.param(
                "b_aq",
                nn.with_partitioning(b_init, MESH_AXES["YN"], self.global_mesh),
                shapes["HD"],
                self.hps.param_dtype,
            )
            bk = self.param(
                "b_ak",
                nn.with_partitioning(b_init, b_kv_mesh_axes, self.global_mesh),
                b_kv_shape,
                self.hps.param_dtype,
            )
            bv = self.param(
                "b_av",
                nn.with_partitioning(b_init, b_kv_mesh_axes, self.global_mesh),
                b_kv_shape,
                self.hps.param_dtype,
            )
            bo = self.param(
                "b_ao",
                nn.with_partitioning(b_init, MESH_AXES["Y"], self.global_mesh),
                shapes["M"],
                self.hps.param_dtype,
            )

        kv_mesh_axes = MESH_AXES["XNNN"] if self.hps.kv_mqa else MESH_AXES["XYNN"]
        q = jnp.einsum("bim,mhd->bhid", x, wq.astype(self.hps.dtype))
        k = jnp.einsum("bim,mhd->bhid", x, wk.astype(self.hps.dtype))
        v = jnp.einsum("bim,mhd->bhid", x, wv.astype(self.hps.dtype))
        q = sharding_constraint(q, MESH_AXES["XYNN"], self.global_mesh)
        k = sharding_constraint(k, kv_mesh_axes, self.global_mesh)
        v = sharding_constraint(v, kv_mesh_axes, self.global_mesh)
        if self.hps.proj_biases:
            q += jnp.expand_dims(bq.astype(self.hps.dtype), (0, 2))  # noqa
            k += jnp.expand_dims(bk.astype(self.hps.dtype), (0, 2))  # noqa
            v += jnp.expand_dims(bv.astype(self.hps.dtype), (0, 2))  # noqa
            q = sharding_constraint(q, MESH_AXES["XYNN"], self.global_mesh)
            k = sharding_constraint(k, kv_mesh_axes, self.global_mesh)
            v = sharding_constraint(v, kv_mesh_axes, self.global_mesh)
        self.sow("intermediates", "aq_l1", coord_check_l1(q))
        self.sow("intermediates", "ak_l1", coord_check_l1(k))
        self.sow("intermediates", "av_l1", coord_check_l1(v))

        if self.hps.qk_norm:
            # maybe should not use with gains since the gains will be tied for all heads
            q = RMSNorm(self.hps, self.global_mesh, "aq")(q)
            k = RMSNorm(self.hps, self.global_mesh, "ak")(k)

        if self.hps.rotary_base > 0:
            q = RotaryEncoding(self.hps, self.global_mesh, is_keys=False)(q)
            k = RotaryEncoding(self.hps, self.global_mesh, is_keys=True)(k)
            q = sharding_constraint(q, MESH_AXES["XYNN"], self.global_mesh)
            k = sharding_constraint(k, kv_mesh_axes, self.global_mesh)
            self.sow("intermediates", "aqr_l1", coord_check_l1(q))
            self.sow("intermediates", "akr_l1", coord_check_l1(k))

        mult = jnp.array([self.hps.qk_scale**0.5], dtype=self.hps.dtype)
        s = jnp.einsum("bhid,bhjd->bhij", q * mult, k * mult)
        s = sharding_constraint(s, MESH_AXES["XYNN"], self.global_mesh)
        self.sow("intermediates", "as_l1", coord_check_l1(s))

        s = CausalMask(self.hps.sequence_len, self.global_mesh)(s)
        s = sharding_constraint(s, MESH_AXES["XYNN"], self.global_mesh)

        p = jax.nn.softmax(s, axis=-1)
        p = sharding_constraint(p, MESH_AXES["XYNN"], self.global_mesh)
        self.sow("intermediates", "ap_l1", coord_check_l1(p))

        o = jnp.einsum("bhij,bhjd->bhid", p, v)
        o = sharding_constraint(o, MESH_AXES["XYNN"], self.global_mesh)
        self.sow("intermediates", "ao_l1", coord_check_l1(o))

        r = jnp.einsum("bhid,hdm->bim", o, wo.astype(self.hps.dtype))
        r = sharding_constraint(r, MESH_AXES["XNY"], self.global_mesh)
        if self.hps.proj_biases:
            r += bo.astype(self.hps.dtype)[None, None, ...]  # noqa
            r = sharding_constraint(r, MESH_AXES["XNY"], self.global_mesh)
        self.sow("intermediates", "ar_l1", coord_check_l1(r))
        return r


class MultiLayerPerceptron(nn.Module):
    hps: TransformerConfig
    global_mesh: jax.sharding.Mesh

    @nn.compact
    def __call__(self, x):
        d_ff_in = int(self.hps.ff_multiple * self.hps.d_model)
        if self.hps.act_name == "swiglu":
            d_ff_in = (d_ff_in // 2) * 2
            d_ff_out = d_ff_in // 2
        else:
            d_ff_out = d_ff_in

        shapes = Dimensions(
            B=x.shape[0],
            T=self.hps.sequence_len,
            M=self.hps.d_model,
            E=d_ff_in,
            F=d_ff_out,
        )
        x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)
        self.sow("intermediates", "fx_l1", coord_check_l1(x))

        i_init = init.normal(self.hps.d_model**-0.5)
        o_init = {
            "zero": init.zeros,
            "vs": init.normal(d_ff_out**-0.5),
        }[self.hps.r_init]
        b_init = init.zeros

        wi = self.param(
            "w_fi",
            nn.with_partitioning(i_init, MESH_AXES["XY"], self.global_mesh),
            shapes["ME"],
            self.hps.param_dtype,
        )
        wo = self.param(
            "w_fo",
            nn.with_partitioning(o_init, MESH_AXES["YX"], self.global_mesh),
            shapes["FM"],
            self.hps.param_dtype,
        )
        if self.hps.proj_biases:
            bi = self.param(
                "b_fi",
                nn.with_partitioning(b_init, MESH_AXES["Y"], self.global_mesh),
                shapes["E"],
                self.hps.param_dtype,
            )
            bo = self.param(
                "b_fo",
                nn.with_partitioning(b_init, MESH_AXES["Y"], self.global_mesh),
                shapes["M"],
                self.hps.param_dtype,
            )

        x = jnp.einsum("btm,me->bte", x, wi.astype(self.hps.dtype))
        x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)
        if self.hps.proj_biases:
            x += bi.astype(self.hps.dtype)[None, None, ...]  # noqa
            x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)
        self.sow("intermediates", "fh_l1", coord_check_l1(x))

        if self.hps.act_name == "swiglu":
            # a more communication-efficient implementation of swiglu would define
            # two separate projections for xg, xf with the same sharding.
            xg, xf = jnp.split(x, 2, axis=-1)
            x = jax.nn.silu(xg) * xf
        else:
            x = getattr(jax.nn, self.hps.act_name)(x)
        x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)

        if self.hps.act_square:
            x = jnp.square(x)
            x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)
        self.sow("intermediates", "fa_l1", coord_check_l1(x))

        x = jnp.einsum("btf,fm->btm", x, wo.astype(self.hps.dtype))
        x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)
        if self.hps.proj_biases:
            x += bo.astype(self.hps.dtype)[None, None, ...]  # noqa
            x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)
        self.sow("intermediates", "fr_l1", coord_check_l1(x))
        return x


class TransformerBlock(nn.Module):
    hps: TransformerConfig
    global_mesh: jax.sharding.Mesh

    @nn.compact
    def __call__(self, x, _):
        kws = dict(hps=self.hps, global_mesh=self.global_mesh)

        x += MultiHeadAttention(**kws)(RMSNorm(**kws, suffix="a")(x))
        x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)

        x += MultiLayerPerceptron(**kws)(RMSNorm(**kws, suffix="f")(x))
        x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)

        return x, None


class Embedding(nn.Module):
    hps: TransformerConfig
    global_mesh: jax.sharding.Mesh

    @nn.compact
    def __call__(self, x):
        w_emb = self.param(
            "w_e",
            nn.with_partitioning(init.normal(1.0), MESH_AXES["NY"], self.global_mesh),
            [self.hps.n_vocab, self.hps.d_model],
            self.hps.param_dtype,
        )
        x = sharding_constraint(x, MESH_AXES["XN"], self.global_mesh)
        x = jnp.take_along_axis(
            w_emb.astype(self.hps.dtype)[None, ...],  # 1VM
            x[..., None],  # BT1
            axis=1,
        )
        x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)

        if self.hps.e_norm:
            x = RMSNorm(self.hps, self.global_mesh, "e")(x)
            x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)
        return x


class Unembedding(nn.Module):
    hps: TransformerConfig
    global_mesh: jax.sharding.Mesh

    @nn.compact
    def __call__(self, x):
        x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)
        x = RMSNorm(self.hps, self.global_mesh, "u")(x)
        x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)

        stddev = self.hps.d_model**-0.5
        u_init = {
            "zero": init.zeros,
            "sp": init.normal(stddev),
            "mup": init.normal(stddev**2),
        }[self.hps.u_init]
        b_init = init.zeros
        wu = self.param(
            "w_u",
            nn.with_partitioning(u_init, MESH_AXES["YN"], self.global_mesh),
            [self.hps.d_model, self.hps.n_vocab],
            self.hps.param_dtype,
        )
        if self.hps.proj_biases:
            bu = self.param(
                "b_u",
                nn.with_partitioning(b_init, MESH_AXES["N"], self.global_mesh),
                [self.hps.n_vocab],
                self.hps.param_dtype,
            )

        if self.hps.is_train:
            output_logits_dtype = self.hps.dtype
        else:
            output_logits_dtype = self.hps.param_dtype

        x = jnp.einsum("btm,mv->btv", x, wu.astype(output_logits_dtype))
        x = sharding_constraint(x, MESH_AXES["XNN"], self.global_mesh)
        if self.hps.proj_biases:
            x += bu.astype(output_logits_dtype)[None, None, ...]  # noqa
            x = sharding_constraint(x, MESH_AXES["XNN"], self.global_mesh)
        return x


class Transformer(nn.Module):
    hps: TransformerConfig
    global_mesh: jax.sharding.Mesh

    @nn.compact
    def __call__(self, x):
        x = jnp.pad(x[:, 0:-1], ((0, 0), (1, 0)), constant_values=self.hps.bos_token_id)
        x = nnp.remat(Embedding)(self.hps, self.global_mesh)(x)
        x, _ = nn.scan(
            nnp.remat(TransformerBlock),
            length=self.hps.n_layer,
            variable_axes=dict(params=0, intermediates=0),
            variable_broadcast=False,
            split_rngs=dict(params=True),
            metadata_params={nn.PARTITION_NAME: None},
        )(hps=self.hps, global_mesh=self.global_mesh)(x, None)
        x = sharding_constraint(x, MESH_AXES["XNY"], self.global_mesh)
        x = nnp.remat(Unembedding)(self.hps, self.global_mesh)(x)
        return x
