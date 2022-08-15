from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from itertools import count
from typing import TYPE_CHECKING, Any, Callable, Generic, Literal, Type, TypeVar, cast, overload

import vapoursynth as vs

from .backends import PyBackend
from .coroutines import frame_eval_async, get_frame, get_frames
from .types import SupportsKeysAndGetItem

__all__ = [
    'PyPlugin',
    'FD_T',
    'PyPluginUnavailableBackend'
]


FD_T = TypeVar('FD_T', bound=Any | SupportsKeysAndGetItem[str, object] | None)
F = TypeVar('F', bound=Callable[..., vs.VideoNode])


@dataclass
class PyPluginOptions:
    float_processing: bool | Literal[16, 32] = False
    shift_chroma: bool = False

    @overload
    def norm_clip(self, clip: vs.VideoNode) -> vs.VideoNode:
        ...

    @overload
    def norm_clip(self, clip: None) -> None:
        ...

    def norm_clip(self, clip: vs.VideoNode | None) -> vs.VideoNode | None:
        if not clip:
            return clip

        assert (fmt := clip.format)

        if self.float_processing:
            bps = 32 if self.float_processing is True else self.float_processing

            if fmt.sample_type is not vs.FLOAT or fmt.bits_per_sample != bps:
                clip = clip.resize.Point(
                    format=fmt.replace(sample_type=vs.FLOAT, bits_per_sample=bps).id,
                    dither_type='none'
                )

        if self.shift_chroma:
            if fmt.sample_type is not vs.FLOAT and not self.float_processing:
                raise ValueError(
                    f'{self.__class__.__name__}: You need to have a clip with float sample type for shift_chroma=True!'
                )

            if fmt.num_planes == 3:
                clip = clip.std.Expr(['', 'x 0.5 +'])

        return clip

    def ensure_output(self, plugin: PyPlugin[FD_T], clip: vs.VideoNode) -> vs.VideoNode:
        assert plugin.ref_clip.format

        if plugin.out_format.id != plugin.ref_clip.format.id:
            return clip.resize.Bicubic(format=plugin.out_format.id, dither_type='none')

        return clip


class PyPluginBase(Generic[FD_T]):
    @staticmethod
    def ensure_output(func: F) -> F:
        @wraps(func)
        def _wrapper(self: PyPlugin[FD_T], *args: Any, **kwargs: Any) -> Any:
            return self.options.ensure_output(self, func(self, *args, **kwargs))

        return cast(F, _wrapper)


class PyPlugin(PyPluginBase[FD_T]):
    if TYPE_CHECKING:
        __slots__ = (
            'backend', 'filter_data', 'clips', 'ref_clip', 'fd',
            '_input_per_plane', 'out_format', 'output_per_plane',
            'is_single_plane'
        )
    else:
        __slots__ = (
            'backend', 'filter_data', 'clips', 'ref_clip', 'fd',
            '_input_per_plane'
        )

    backend: PyBackend
    filter_data: Type[FD_T]

    options: PyPluginOptions = PyPluginOptions()

    input_per_plane: bool | list[bool] = True
    output_per_plane: bool = True
    channels_last: bool = True

    min_clips: int = 1
    max_clips: int = -1

    clips: list[vs.VideoNode]
    ref_clip: vs.VideoNode
    out_format: vs.VideoFormat

    fd: FD_T

    if TYPE_CHECKING:
        def process(self, f: vs.VideoFrame, src: Any, dst: Any, plane: int | None, n: int) -> None:
            raise NotImplementedError
    else:
        process: FDC_SELF[FD_T]

    def __class_getitem__(cls, fdata: Type[FD_T] | None = None) -> Type[PyPlugin[FD_T]]:
        class PyPluginInnerClass(cls):  # type: ignore
            filter_data = fdata

        return PyPluginInnerClass

    def __init__(
        self, ref_clip: vs.VideoNode, clips: list[vs.VideoNode] | None = None, **kwargs: Any
    ) -> None:
        assert ref_clip.format

        self.out_format = ref_clip.format

        self.ref_clip = self.options.norm_clip(ref_clip)

        self.clips = [self.options.norm_clip(clip) for clip in clips] if clips else []

        try:
            self.fd = self.filter_data(**kwargs)  # type: ignore
        except BaseException:
            self.fd = None  # type: ignore

        n_clips = 1 + len(self.clips)

        class_name = self.__class__.__name__

        input_per_plane = self.input_per_plane

        if not isinstance(input_per_plane, list):
            input_per_plane = [input_per_plane]

        for _ in range((1 + len(self.clips)) - len(input_per_plane)):
            input_per_plane.append(input_per_plane[-1])

        self._input_per_plane = input_per_plane

        if ref_clip.format.num_planes == 1:
            self.output_per_plane = True

        self.is_single_plane = [
            bool(clip.format and clip.format.num_planes == 1)
            for clip in (self.ref_clip, *self.clips)
        ]

        if n_clips < self.min_clips or (self.max_clips > 0 and n_clips > self.max_clips):
            max_clips_str = 'inf' if self.max_clips == -1 else self.max_clips
            raise ValueError(
                f'{class_name}: You must pass {self.min_clips} <= n clips <= {max_clips_str}!'
            )

        if not self.output_per_plane and (ref_clip.format.subsampling_w or ref_clip.format.subsampling_h):
            raise ValueError(
                f'{class_name}: You can\'t have output_per_plane=False with a subsampled clip!'
            )

        for idx, clip, ipp in zip(count(-1), (self.ref_clip, *self.clips), self._input_per_plane):
            assert clip.format
            if not ipp and (clip.format.subsampling_w or clip.format.subsampling_h):
                clip_type = 'Ref Clip' if idx == -1 else f'Clip Index: {idx}'
                raise ValueError(
                    f'{class_name}: You can\'t have input_per_plane=False with a subsampled clip! ({clip_type})'
                )

    @PyPluginBase.ensure_output
    def invoke(self) -> vs.VideoNode:
        assert self.ref_clip.format

        def _stack_frame(frame: vs.VideoFrame, idx: int) -> memoryview | list[memoryview]:
            return frame[0] if self.is_single_plane[idx] else [frame[p] for p in {0, 1, 2}]

        if self.output_per_plane:
            if self.clips:
                @frame_eval_async(self.ref_clip)
                async def output(n: int) -> vs.VideoFrame:
                    frames = await get_frames(self.ref_clip, *self.clips, frame_no=n)
                    fout = frames[0].copy()

                    pre_stacked_clips = {
                        idx: _stack_frame(frame, idx)
                        for idx, frame in enumerate(frames)
                        if not self._input_per_plane[idx]
                    }

                    for p in range(fout.format.num_planes):
                        inputs_data = [
                            frame[p] if self._input_per_plane[idx] else pre_stacked_clips[idx]
                            for idx, frame in enumerate(frames)
                        ]

                        self.process(fout, inputs_data, fout[p], p, n)

                    return fout
            else:
                if self._input_per_plane[0]:
                    @frame_eval_async(self.ref_clip)
                    async def output(n: int) -> vs.VideoFrame:
                        frame = await get_frame(self.ref_clip, n)
                        fout = frame.copy()

                        for p in range(fout.format.num_planes):
                            self.process(fout, frame[p], fout[p], p, n)

                        return fout
                else:
                    @frame_eval_async(self.ref_clip)
                    async def output(n: int) -> vs.VideoFrame:
                        ref_frame = await get_frame(self.ref_clip, n)
                        fout = ref_frame.copy()

                        pre_stacked_clip = _stack_frame(ref_frame, 0)

                        for p in range(fout.format.num_planes):
                            self.process(fout, pre_stacked_clip, fout[p], p, n)

                        return fout
        else:
            if self.clips:
                @frame_eval_async(self.ref_clip)
                async def output(n: int) -> vs.VideoFrame:
                    frames = await get_frames(self.ref_clip, *self.clips, frame_no=n)
                    fout = frames[0].copy()

                    src_arrays = [_stack_frame(frame, idx) for idx, frame in enumerate(frames)]

                    self.process(fout, src_arrays, fout, None, n)

                    return fout
            else:
                if self.ref_clip.format.num_planes == 1:
                    @frame_eval_async(self.ref_clip)
                    async def output(n: int) -> vs.VideoFrame:
                        frame = await get_frame(self.ref_clip, n)
                        fout = frame.copy()

                        self.process(fout, frame[0], fout[0], 0, n)

                        return fout
                else:
                    @frame_eval_async(self.ref_clip)
                    async def output(n: int) -> vs.VideoFrame:
                        frame = await get_frame(self.ref_clip, n)
                        fout = frame.copy()

                        self.process(fout, frame, fout, None, n)

                        return fout

        return output


class PyPluginUnavailableBackend(PyPlugin[FD_T]):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        from .exceptions import UnavailableBackend

        raise UnavailableBackend(self.backend, self)
