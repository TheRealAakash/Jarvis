import logging
import jax  
from jax.experimental.pjit import pjit
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple, Union
from flax.linen import partitioning as flax_partitioning
from jax.experimental.maps import Mesh
from jax.experimental.mesh_utils import create_hybrid_device_mesh

from flax import traverse_util

from states import TrainState

import numpy as np
import typing
import cached_property

JaxDevice = jax.lib.xla_client.Device

TpuMesh = Tuple[int, int, int, int]  # (x, y, z, num_cores).
OtherMesh = Tuple[int, int]
HardwareMesh = Union[TpuMesh, OtherMesh]
LogicalAxisRules = Sequence[Tuple[str, Optional[str]]]

PartitionedCallable = Callable[..., Any]


# pjit Mesh creation functions.
# -----------------------------------------------------------------------------
def bounds_from_last_device(
        last_device: jax.lib.xla_client.Device) -> HardwareMesh:
    """Get the bound from the given last device."""
    # Must be passed the device at the highest-coordinate corner of the
    # relevant mesh, which is a requirement we know is satisfied by the last
    # device in jax.devices().
    if hasattr(last_device, 'coords'):
        x, y, z = last_device.coords
        return x + 1, y + 1, z + 1, last_device.core_on_chip + 1
    else:
        # On non-TPU platforms, the "mesh" is hosts x devices per host in order
        # to take advantage of faster within-host interconnect.
        return jax.host_count(), jax.local_device_count()


def get_coords(device: jax.lib.xla_client.Device) -> HardwareMesh:
    """Returns the coordinates of the given device."""
    if hasattr(device, 'coords'):
        return (*device.coords, device.core_on_chip)
    return (device.process_index, device.id % jax.local_device_count())


def global_mesh_defined():
    """Checks if global xmap/pjit mesh resource environment is defined."""
    maps_env = jax.experimental.maps.thread_resources.env
    return maps_env.physical_mesh.devices.shape != ()  # pylint: disable=g-explicit-bool-comparison


def get_mesh(model_parallel_submesh: HardwareMesh,
             input_devices: Sequence[JaxDevice] = (),
             input_local_devices: Sequence[JaxDevice] = (),
             tile_by_host_if_needed: bool = True,
             backend: Optional[str] = None) -> Mesh:
    """Construct an xmap/pjit Mesh for the given model-parallel submesh.

    The resulting mesh has two resource axes: 'model', with the provided submesh
    shape, and 'data', which covers the rest of the mesh.

    Args:
      model_parallel_submesh: a HardwareMesh spec, namely (x,y,z,core) on TPU for
        a single model-parallel replica's "tile" in the physical device mesh. The
        first three elements (`x`, `y`, and `z`) should be factors of the pod
        slice; e.g., if you are using df_4x8, then `x` should be a factor of 4
        (one of 1, 2, 4), `y` should be a factor of 8 (one of 1, 2, 4, 8), and `z`
        must be 1, because TPU v3 slices are only 2D. `z` can be >1 for TPU v4
        (and maybe later TPUs) that allow 3D slices. `core` is the number of cores
        to use from each TPU node. As communication is usually fastest inside the
        same node, if you need a tile of more than 1 core, then
        you should first increase `core`: e.g., for TPU v3, (1,1,1,2) is better
          than (2,1,1,1). To pick a good spec, try a few possible values until you
          get high TPU utilization.
      input_devices: the devices to use, will use jax.devices() if this is not
        set.
      input_local_devices: the local devices to use, will use jax.local_devices()
        if this is not set.
      tile_by_host_if_needed: JAX currently requires that the parts of any sharded
        array that are located on one host's local devices form a single
        contiguous slice. A best effort will be made to achieve this without
        "tiling" the device assignment over hosts (which can reduce XLA collective
        performance). If this flag is True, then the device assignment will be
        tiled over hosts if necessary to satisfy this constraint and create a
        buildable mesh; if false, mesh construction will fail instead.
      backend: get devices from the pinned backend, if specified. This is
        useful for explicitly specifying the devices other than relying on
        jax_platform_name.

    Returns:
      A xmap / pjit Mesh containing the virtual device mesh with data, model axes.
    """
    input_devices = input_devices or jax.devices(backend)
    input_local_devices = input_local_devices or jax.local_devices(0, backend)
    # Sort input_devices based on coords, as backends might not return devices
    # in order.
    last_device = sorted(input_devices, key=get_coords)[-1]
    last_input_local_devices = sorted(input_local_devices, key=get_coords)[-1]
    logging.info('last device coords : %r\nlast local device coords: %r',
                 get_coords(last_device), get_coords(last_input_local_devices))
    global_hardware_mesh = bounds_from_last_device(last_device)
    mesh_ndim = len(global_hardware_mesh)
    local_hardware_mesh = bounds_from_last_device(last_input_local_devices)
    mesh_err = (
        f'each dimension of the model parallel submesh {model_parallel_submesh} '
        'must be a factor of the corresponding dimension of the global device '
        f'mesh {global_hardware_mesh}')
    assert not any(
        g % m
        for g, m in zip(global_hardware_mesh, model_parallel_submesh)), mesh_err
    assert not any(
        g % l for g, l in zip(global_hardware_mesh, local_hardware_mesh))
    devices = np.empty(global_hardware_mesh, dtype=object)
    for device in input_devices:
        device_coords = get_coords(device)
        devices[device_coords] = device
    tile_by_host = tile_by_host_if_needed
    if len(global_hardware_mesh) == 4:
        # enable contiguous local chunks without host tiling by making Z major
        global_hardware_mesh = typing.cast(Tuple[int, int, int, int],
                                           global_hardware_mesh)
        model_parallel_submesh = typing.cast(Tuple[int, int, int, int],
                                             model_parallel_submesh)
        gx, gy, gz, gc = global_hardware_mesh
        mx, my, mz, mc = model_parallel_submesh
        if (mx == gx > 1 and my == mz == 1) or (mx == 1 and my == gy > 1 and
                                                mz == gz > 1):
            logging.info('ensuring YZ plane has a Z-major device order')
            # YZ should be ZY
            assert mc == gc, (mc, gc)
            global_hardware_mesh = gx, gz, gy, gc
            model_parallel_submesh = mx, mz, my, mc
            devices = devices.swapaxes(1, 2)
            tile_by_host = False
        if (my == gy > 1 and mx == mz == 1) or (my == 1 and mx == gx > 1 and
                                                mz == gz > 1):
            logging.info('ensuring XZ plane has a Z-major device order')
            # XZ should be ZX
            assert mc == gc, (mc, gc)
            global_hardware_mesh = gz, gy, gx, gc
            model_parallel_submesh = mz, my, mx, mc
            devices = devices.swapaxes(0, 2)
            tile_by_host = False
    if tile_by_host:
        logging.warning(
            'Tiling device assignment mesh by hosts, which may lead to '
            'reduced XLA collective performance. To avoid this, modify '
            'the model parallel submesh or run with more tasks per host.')
        tile_err = (
            'to tile the mesh by hosts, each dimension of the model parallel '
            'submesh must be either a factor or a multiple of the corresponding '
            'dimension of the per-host submesh')

        def dh_dd_mh_md(g: int, m: int, l: int) -> Tuple[int, int, int, int]:
            """Split a global mesh dimension into four tiling components.

            Args:
              g: global mesh bounds dimension size
              m: model-parallel submesh bounds dimension size
              l: local submesh bounds dimension size

            Returns:
              The resulting tuple divides the dimension into the hosts component of
              the data-parallel submesh, the devices component of the data-parallel
              submesh, the hosts component of the model-parallel submesh, and the
              devices component of the model-parallel submesh.
            """
            d = g // m
            if m >= l:
                assert not m % l, tile_err
                return (d, 1, m // l, l)
            else:
                assert not l % m, tile_err
                return (d // (l // m), l // m, 1, m)

        # e.g. [(x_data_hosts, x_data_devs, x_model_hosts, x_model_devs), ...]
        dh_dd_mh_md_tups = map(dh_dd_mh_md, global_hardware_mesh,
                               model_parallel_submesh, local_hardware_mesh)
        # reshape to e.g. (x_dh, x_dd, x_mh, x_md, y_dh, ...)
        devices = devices.reshape(*(s for t in dh_dd_mh_md_tups for s in t))  # pylint: disable=g-complex-comprehension
        # TODO(jekbradbury): reorder local subgroups for ring locality
        # Transpose to [data_host], [data_device], [model_host], [model_device]
        # block ordering e.g. (x_dh, y_dh, ..., x_dd, y_dd, ...)
        devices = devices.transpose(*(4 * i for i in range(mesh_ndim)),
                                    *(4 * i + 1 for i in range(mesh_ndim)),
                                    *(4 * i + 2 for i in range(mesh_ndim)),
                                    *(4 * i + 3 for i in range(mesh_ndim)))
    else:
        # e.g. [(x_data, x_model), (y_data, y_model), ...]
        model_data_tups = [
            (g // m, m)
            for g, m in zip(global_hardware_mesh, model_parallel_submesh)
        ]
        # reshape to e.g. (x_data, x_model, y_data, y_model...)
        devices = devices.reshape(*(s for t in model_data_tups for s in t))  # pylint: disable=g-complex-comprehension
        # TODO(jekbradbury): reorder small subgroups for ring locality
        # transpose to e.g. (x_data, y_data, ..., x_model, ...)
        devices = devices.transpose(*(2 * i for i in range(mesh_ndim)),
                                    *(2 * i + 1 for i in range(mesh_ndim)))
    # reshape to (data, model)
    devices = devices.reshape(-1, np.prod(model_parallel_submesh))
    global_mesh = Mesh(devices, ['data', 'model'])
    logging.info('global_mesh axis_names: %s', global_mesh.axis_names)
    logging.info('global_mesh devices: %s', global_mesh.devices)
    logging.info('global_mesh devices shape: %s', global_mesh.devices.shape)
    return global_mesh


def get_cpu_mesh() -> Mesh:
    """Trivial mesh for CPU Testing."""
    devices = np.empty((jax.host_count(), jax.local_device_count()), dtype=object)
    for device in jax.devices():
        devices[device.process_index, device.id % jax.local_device_count()] = device
    return Mesh(devices, ['data', 'model'])


def get_gpu_mesh(num_partitions: int) -> Mesh:
    """Mesh for GPUs that preferentially places 'model' on NVLink."""
    nvlink_size = jax.local_device_count()
    dcn_size = jax.process_count()
    nvlink_mp = min(num_partitions, nvlink_size)
    nvlink_dp, extra1 = divmod(nvlink_size, nvlink_mp)
    dcn_mp, extra2 = divmod(num_partitions, nvlink_mp)
    assert not (extra1 or extra2), ('number of partitions on GPU must be a factor'
                                    ' or multiple of the number of local devices')
    dcn_dp = dcn_size // dcn_mp

    devices = create_hybrid_device_mesh(
        mesh_shape=[nvlink_dp, nvlink_mp],
        dcn_mesh_shape=[dcn_dp, dcn_mp],
        process_is_granule=True)

    global_mesh = Mesh(devices, ['data', 'model'])
    logging.info('global_mesh axis_names: %s', global_mesh.axis_names)
    logging.info('global_mesh devices: %s', global_mesh.devices)
    return global_mesh


def default_mesh(num_partitions: int,
                 model_parallel_submesh: Optional[HardwareMesh] = None,
                 backend: Optional[str] = None) -> Mesh:
    """Attempt to return a default mesh for simple cases.

    Args:
      num_partitions: number of partitions to use, will be ignored if
        model_parallel_submesh is provided.
      model_parallel_submesh: 4-tuple that specifies the x,y,z,c submesh to use as
        the model-parallel device tile.
      backend: get devices from the pinned backend, if specified. This is useful
        for explicitly specifying the devices other than relying on
        jax_platform_name.

    Returns:
      xmap/pjit 2D Mesh with 'data', 'model' mesh axes.
    """
    last_device = jax.devices(backend)[-1]
    platform = last_device.platform
    device_kind = last_device.device_kind
    bounds = bounds_from_last_device(last_device)

    if model_parallel_submesh:
        return get_mesh(model_parallel_submesh, backend=backend)

    if platform == 'cpu':
        return get_cpu_mesh()
    elif platform == 'gpu':
        return get_gpu_mesh(num_partitions)

    mps = None
    if device_kind in ('TPU v2', 'TPU v3'):
        if num_partitions == 1:
            mps = (1, 1, 1, 1)
        elif num_partitions == 2:
            mps = (1, 1, 1, 2)
        elif num_partitions == 4:
            mps = (2, 1, 1, 2)
        elif num_partitions == 8:
            mps = (2, 2, 1, 2)
        elif num_partitions == 16:
            mps = (4, 2, 1, 2)
    # assume the use of megacore on TPU v4
    elif (device_kind == 'TPU v4' or
          device_kind == 'TPU v4 lite') and bounds[3] == 1:
        if num_partitions == 1:
            mps = (1, 1, 1, 1)
        elif num_partitions == 2:
            mps = (1, 2, 1, 1)
        elif num_partitions == 4:
            if bounds[0] >= 4:
                mps = (4, 1, 1, 1)
            else:
                mps = (2, 2, 1, 1)
        elif num_partitions == 8:
            if bounds[2] >= 8:
                mps = (1, 1, 8, 1)
            else:
                mps = (4, 2, 1, 1)
        elif num_partitions == 16:
            if bounds[2] >= 16:
                mps = (1, 1, 16, 1)
            elif bounds[0] >= 8:
                mps = (8, 2, 1, 1)
            elif bounds[0] >= 4:
                mps = (4, 4, 1, 1)
            else:
                mps = (2, 2, 4, 1)

    if mps is None:
        raise ValueError('No default mesh for this configuration: specify '
                         'config.model_parallel_submesh explicitly.')
    return get_mesh(mps, backend=backend)

# T5X pjit partitioning
class PjittedFnWithContext(PartitionedCallable):
    """Wraps pjitted function to apply the appropriate contexts."""

    def __init__(self,
                 pjitted_fn,
                 partition_mesh: Mesh,
                 logical_axis_rules: flax_partitioning.LogicalRules = ()):
        self._pjitted_fn = pjitted_fn
        self._mesh = partition_mesh
        self._logical_axis_rules = logical_axis_rules

    def __call__(self, *args):
        with Mesh(self._mesh.devices,
                  self._mesh.axis_names), flax_partitioning.axis_rules(
                      self._logical_axis_rules):
            return self._pjitted_fn(*args)

    def lower(self, *args):
        with Mesh(self._mesh.devices, self._mesh.axis_names), flax_partitioning.axis_rules(self._logical_axis_rules):
            return self._pjitted_fn.lower(*args)


class Partitioner:
    def __init__(
        self,
        num_partitions: Optional[int] = None,
        model_parallel_submesh: Optional[HardwareMesh] = None,
        params_on_devices: bool = True,
        backend: Optional[str] = None,
        logical_axis_rules: Optional[LogicalAxisRules] = None,
        use_cpu_pjit: Optional[bool] = False
    ):
        if not num_partitions and not model_parallel_submesh:
            raise ValueError('At least one of `num_partitions` or '
                       '`model_parallel_submesh` must be set.')

        if model_parallel_submesh is not None and len(model_parallel_submesh) != 4:
            logging.error(
                '`model_parallel_submesh` must be either None or a 4-tuple. Got '
                'Got `num_partitions=%s`. A ValueError will be raised beginning '
                'March 1, 2022.', model_parallel_submesh)

        if bool(num_partitions) and bool(model_parallel_submesh):
            logging.error(
                'At most one of `num_partitions` or `model_parallel_submesh` can be '
                'set. Got `num_partitions=%s` and `model_parallel_submesh`=%s. A '
                'ValueError will be raised beginning March 21, 2022.', num_partitions,
                model_parallel_submesh)
        
        self._num_partitions = num_partitions
        self._model_parallel_submesh = model_parallel_submesh
        self._params_on_devices = params_on_devices
        self._data_axis = 'data'
        self._backend = backend

        assert logical_axis_rules is not None, "logical_axis_rules must be set."
                
        self._logical_axis_rules = tuple(logical_axis_rules)
        self._data_axis, = flax_partitioning.logical_to_mesh_axes(
            ['batch'], logical_axis_rules)
        self._use_cpu_pjit = use_cpu_pjit
    
    @property
    def mesh(self) -> Mesh:
        return default_mesh(self._num_partitions, self._model_parallel_submesh,
                            self._backend)

    def partition(
        self,
        fn: Callable,  # pylint: disable=g-bare-generic
        in_axis_resources,
        out_axis_resources,
        static_argnums: Union[int, Sequence[int]] = (),
        donate_argnums: Union[int, Sequence[int]] = ()
    ):
        pjitted = pjit(
            fn,
            in_axis_resources=in_axis_resources,
            out_axis_resources=out_axis_resources,
            static_argnums=static_argnums,
            donate_argnums=donate_argnums,
        )

        return PjittedFnWithContext(pjitted, self.mesh, self._logical_axis_rules)

    def get_logical_axes(self, train_state: TrainState) -> TrainState:
        """Returns a copy of TrainState with Optional[AxisNames] as leaves."""
        return train_state.as_logical_axes()

        
    def get_mesh_axes(self, train_state: TrainState) -> TrainState:
        """Returns a copy of TrainState with Optional[PartitionSpecs] as leaves."""
        logical_axes = self.get_logical_axes(train_state)

        def _logical_to_mesh_axes(param_name, logical_axes):
            if logical_axes is None:
                return None
            elif logical_axes is traverse_util.empty_node:
                return traverse_util.empty_node
            try:
                return flax_partitioning.logical_to_mesh_axes(logical_axes,
                                                            self._logical_axis_rules)
            except ValueError as e:
                raise ValueError(f'Failed to map logical axes for {param_name}') from e

        flat_logical_axes = traverse_util.flatten_dict(
            logical_axes.state_dict(), keep_empty_nodes=True, sep='/')
        flat_mesh_axes = {
            k: _logical_to_mesh_axes(k, v) for k, v in flat_logical_axes.items()
        }
        
        return logical_axes.restore_state(
            traverse_util.unflatten_dict(flat_mesh_axes, sep='/'))

    