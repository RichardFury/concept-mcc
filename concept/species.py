# This file is part of CO𝘕CEPT, the cosmological 𝘕-body code in Python.
# Copyright © 2015–2019 Jeppe Mosgaard Dakin.
#
# CO𝘕CEPT is free software: You can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# CO𝘕CEPT is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with CO𝘕CEPT. If not, see http://www.gnu.org/licenses/
#
# The author of CO𝘕CEPT can be contacted at dakin(at)phys.au.dk
# The latest version of CO𝘕CEPT is available at
# https://github.com/jmd-dk/concept/



# Import everything from the commons module.
# In the .pyx file, Cython declared variables will also get cimported.
from commons import *

# Cython imports
cimport('from analysis import measure')
cimport('from communication import '
    'communicate_domain, domain_subdivisions, exchange, smart_mpi, '
    'domain_size_x, domain_size_y, domain_size_z, domain_start_x, domain_start_y, domain_start_z, '
)
cimport('from fluid import maccormack, maccormack_internal_sources, '
    'kurganov_tadmor, kurganov_tadmor_internal_sources'
)
cimport('from integration import Spline, cosmic_time, scale_factor, ȧ')
cimport('from linear import compute_cosmo, compute_transfer, get_default_k_parameters, realize')



# Class which serves as the data structure for fluid variables,
# efficiently and elegantly implementing symmetric
# multi-dimensional arrays. The actual fluid (scalar) grids are then
# the elements of these tensors.
class Tensor:
    """With respect to indexing and looping, you may threat instances of
    this class just like NumPy arrays.
    """
    def __init__(self, component, varnum, shape, symmetric=False, active=True):
        """If disguised_scalar is True, every element of the tensor
        will always point to the same object.
        """
        # Empty and otherwise falsy shapes are understood as rank 0
        # tensors (scalars). For this reason,
        # empty tensors are not representable.
        if not shape:
            shape = 1
        # Store initialization arguments as instance variables
        self.component = component
        self.varnum = varnum
        self.shape = tuple(any2list(shape))
        self.symmetric = symmetric
        self.active = active
        # Store other array-like attributes
        self.size = np.prod(self.shape)
        self.ndim = len(self.shape)
        self.dtype = 'object'
        # Is this tensor really just a scalar in disguise?
        self.disguised_scalar = (self.component.boltzmann_order + 1 == self.varnum)
        # Should this fluid variable do realizations when iterating
        # with the iterate method?
        self.iterative_realizations = (
            self.disguised_scalar and self.component.boltzmann_closure == 'class'
        )
        # Only "square" tensors can be symmetric
        if self.symmetric and len(set(self.shape)) != 1:
            abort('A {} tensor cannot be made symmetric'.format('×'.join(self.shape)))
        # Compute and store all multi_indices
        self.multi_indices = tuple(sorted(set([self.process_multi_index(multi_index)
                                               for multi_index
                                               in itertools.product(*[range(size)
                                                                      for size in self.shape])])))
        # Initialize tensor data
        self.data = {multi_index: None for multi_index in self.multi_indices}
        # Additional (possible) elements
        self.additional_dofs = ('trace', )
        for additional_dof in self.additional_dofs:
            self.data[additional_dof] = None
    # Method for processing multi_indices.
    # This is where the symmetry of the indices is implemented.
    def process_multi_index(self, multi_index):
        # The case of an extra degree of freedom
        if isinstance(multi_index, str):
            return multi_index.lower()
        if (    isinstance(multi_index, tuple)
            and len(multi_index) == 1
            and isinstance(multi_index[0], str)):
            return multi_index[0].lower()
        # The normal case
        if self.symmetric:
            multi_index = tuple(sorted(any2list(multi_index), reverse=True))
        else:
            multi_index = tuple(any2list(multi_index))
        if len(multi_index) != self.ndim:
            # The tensor has been indexed with a non-existing index
            raise KeyError(f'An attempt was made to index {self} using {multi_index}')
        return multi_index
    # Methods for indexing and membership testing
    def __getitem__(self, multi_index):
        multi_index = self.process_multi_index(multi_index)
        return self.data[multi_index]
    def __setitem__(self, multi_index, value):
        multi_index = self.process_multi_index(multi_index)
        if not multi_index in self.data:
            abort('Attempted to access multi_index {} of {} tensor'
                  .format(multi_index, '×'.join(self.shape))
                  )
        self.data[multi_index] = value
        # If only a single element should exist in memory,
        # point every multi_index to the newly set element.
        if self.disguised_scalar and multi_index not in self.additional_dofs:
            for other_multi_index in self.multi_indices:
                self.data[other_multi_index] = value
    def __contains__(self, multi_index):
        try:
           multi_index = self.process_multi_index(multi_index)
        except (IndexError, KeyError):
            return False
        return multi_index in self.data
    # Iteration
    def __iter__(self):
        """By default, iteration yields all elements, except for
        the case of disguised scalars, where only the first element
        is yielded.
        Elements corresponding to additional degrees of freedom
        are not included.
        Inactive tensors do not yield anything.
        """
        if self.active:
            N_elements = 1 if self.disguised_scalar else len(self.multi_indices)
        else:
            N_elements = 0
        return (self.data[multi_index] for multi_index in self.multi_indices[:N_elements])
    def iterate(self, *attributes, multi_indices=False, a_next=-1):
        """This generator yields all normal elements of the tensor
        (that is, the additional degrees of freedom are not included).
        For disguised scalars, all logical elements are realized
        before they are yielded.
        What attribute(s) of the elements (fluidscalars) should be
        yielded is controlled by the attributes argument. For a value of
        'fluidscalar', the entire fluidscalar is returned. This is also
        the default behaviour when no attributes are passed.
        If multi_index is True, both the multi_index and the fluidscalar
        will be yielded, in that order.
        """
        if not attributes:
            attributes = ('fluidscalar', )
        for multi_index in self.multi_indices:
            with unswitch:
                if self.iterative_realizations:
                    self.component.realize_if_linear(
                        self.varnum, specific_multi_index=multi_index, a_next=a_next,
                    )
            fluidscalar = self.data[multi_index]
            values = []
            for attribute in attributes:
                if attribute == 'fluidscalar':
                    values.append(fluidscalar)
                else:
                    values.append(getattr(fluidscalar, attribute))
            with unswitch:
                if multi_indices:
                    yield (multi_index, *values)
                else:
                    if len(values) == 1:
                        yield values[0]
                    else:
                        yield tuple(values)
    # String representation
    def __repr__(self):
        return f'<Tensor({self.component}, {self.varnum}, {self.shape}>'
    def __str__(self):
        return self.__repr__()



# Class which serves as the data structure for a scalar fluid grid
# (each component of a fluid variable is stored as a collection of
# scalar grids). Each scalar fluid has its own name, e.g.
# ϱ    (varnum == 0, multi_index == 0),
# J[0] (varnum == 1, multi_index == 0),
# J[1] (varnum == 1, multi_index == 1),
# J[2] (varnum == 1, multi_index == 2),
@cython.cclass
class FluidScalar:
    # Initialization method
    @cython.header(# Arguments
                   varnum='int',
                   multi_index=object,  # tuple or int-like
                   is_linear='bint',
                   )
    def __init__(self, varnum, multi_index=(), is_linear=False):
        # The triple quoted string below serves as the type declaration
        # for the data attributes of the FluidScalar type.
        # It will get picked up by the pyxpp script
        # and indluded in the .pxd file.
        """
        # Fluid variable number and index of fluid scalar
        public int varnum
        public tuple multi_index
        # The is_linear flag
        public bint is_linear
        # Meta data
        public tuple shape
        public tuple shape_noghosts
        public Py_ssize_t size
        public Py_ssize_t size_noghosts
        # The data itself
        double* grid
        public double[:, :, ::1] grid_mv
        public double[:, :, :] grid_noghosts
        # The starred buffer
        double* gridˣ
        public double[:, :, ::1] gridˣ_mv
        public double[:, :, :] gridˣ_noghosts
        # The Δ buffer
        double* Δ
        public double[:, :, ::1] Δ_mv
        public double[:, :, :] Δ_noghosts
        """
        # Number and index of fluid variable
        self.varnum = varnum
        self.multi_index = tuple(any2list(multi_index))
        # Flag indicating whether this FluidScalar is linear or not.
        # For a linear FluidScalar, the starred and unstarred grids
        # point to the same memory.
        self.is_linear = is_linear
        # Minimal starting layout
        self.shape = (1, 1, 1)
        self.shape_noghosts = (1, 1, 1)
        self.size = np.prod(self.shape)
        self.size_noghosts = np.prod(self.shape_noghosts)
        # The data itself
        self.grid = malloc(self.size*sizeof('double'))
        self.grid_mv = cast(self.grid, 'double[:self.shape[0], :self.shape[1], :self.shape[2]]')
        self.grid_noghosts = self.grid_mv[:, :, :]
        # The starred buffer
        if self.is_linear:
            self.gridˣ          = self.grid
            self.gridˣ_mv       = self.grid_mv
            self.gridˣ_noghosts = self.grid_noghosts
        else:
            self.gridˣ = malloc(self.size*sizeof('double'))
            self.gridˣ_mv = cast(
                self.gridˣ, 'double[:self.shape[0], :self.shape[1], :self.shape[2]]',
            )
            self.gridˣ_noghosts = self.gridˣ_mv[:, :, :]
        # Due to the Unicode NFKC normalization done by pure Python,
        # attributes with a ˣ in their name need to be set in following
        # way in order for dynamical lookup to function.
        if not cython.compiled:
            setattr(self, 'gridˣ'         , self.gridˣ         )
            setattr(self, 'gridˣ_mv'      , self.gridˣ_mv      )
            setattr(self, 'gridˣ_noghosts', self.gridˣ_noghosts)
        # The Δ buffer
        self.Δ = malloc(self.size*sizeof('double'))
        self.Δ_mv = cast(self.Δ, 'double[:self.shape[0], :self.shape[1], :self.shape[2]]')
        self.Δ_noghosts = self.Δ_mv[:, :, :]

    # Method for resizing all grids of this scalar fluid
    @cython.header(# Arguments
                   shape_nopseudo_noghost=tuple,
                   )
    def resize(self, shape_nopseudo_noghost):
        """After resizing the fluid scalar,
        all fluid elements will be nullified.
        """
        # The full shape and size of the grid,
        # with pseudo and ghost points.
        self.shape = tuple([2 + s + 1 + 2 for s in shape_nopseudo_noghost])
        self.size = np.prod(self.shape)
        # The shape and size of the grid
        # with no ghost points but with pseudo points.
        self.shape_noghosts = tuple([s + 1 for s in shape_nopseudo_noghost])
        self.size_noghosts = np.prod(self.shape_noghosts)
        # The data itself
        self.grid = realloc(self.grid, self.size*sizeof('double'))
        self.grid_mv = cast(self.grid, 'double[:self.shape[0], :self.shape[1], :self.shape[2]]')
        self.grid_noghosts = self.grid_mv[
            2:(self.grid_mv.shape[0] - 2),
            2:(self.grid_mv.shape[1] - 2),
            2:(self.grid_mv.shape[2] - 2),
        ]
        # Nullify the newly allocated data grid
        self.nullify_grid()
        # The starred buffer
        if self.is_linear:
            self.gridˣ          = self.grid
            self.gridˣ_mv       = self.grid_mv
            self.gridˣ_noghosts = self.grid_noghosts
        else:
            self.gridˣ = realloc(self.gridˣ, self.size*sizeof('double'))
            self.gridˣ_mv = cast(
                self.gridˣ, 'double[:self.shape[0], :self.shape[1], :self.shape[2]]',
            )
            self.gridˣ_noghosts = self.gridˣ_mv[
                2:(self.gridˣ_mv.shape[0] - 2),
                2:(self.gridˣ_mv.shape[1] - 2),
                2:(self.gridˣ_mv.shape[2] - 2),
            ]
        # Due to the Unicode NFKC normalization done by pure Python,
        # attributes with a ˣ in their name need to be set in following
        # way in order for dynamical lookup to function.
        if not cython.compiled:
            setattr(self, 'gridˣ'         , self.gridˣ         )
            setattr(self, 'gridˣ_mv'      , self.gridˣ_mv      )
            setattr(self, 'gridˣ_noghosts', self.gridˣ_noghosts)
        # Nullify the newly allocated starred buffer
        if not self.is_linear:
            self.nullify_gridˣ()
        # The Δ buffer
        self.Δ = realloc(self.Δ, self.size*sizeof('double'))
        self.Δ_mv = cast(self.Δ, 'double[:self.shape[0], :self.shape[1], :self.shape[2]]')
        self.Δ_noghosts = self.Δ_mv[
            2:(self.Δ_mv.shape[0] - 2),
            2:(self.Δ_mv.shape[1] - 2),
            2:(self.Δ_mv.shape[2] - 2),
        ]
        # Nullify the newly allocated Δ buffer
        self.nullify_Δ()

    # Method for scaling the data grid
    @cython.pheader(# Argument
                    a='double',
                    # Locals
                    i='Py_ssize_t',
                    grid='double*',
                    )
    def scale_grid(self, a):
        # Extract data pointer
        grid = self.grid
        # Scale data buffer
        for i in range(self.size):
            grid[i] *= a

    # Method for nullifying the data grid
    @cython.pheader(# Locals
                    i='Py_ssize_t',
                    grid='double*',
                    )
    def nullify_grid(self):
        # Extract data pointer
        grid = self.grid
        # Nullify data buffer
        for i in range(self.size):
            grid[i] = 0

    # Method for nullifying the starred grid
    @cython.pheader(# Locals
                    i='Py_ssize_t',
                    gridˣ='double*',
                    )
    def nullify_gridˣ(self):
        # Extract starred buffer pointer
        gridˣ = self.gridˣ
        # Nullify starred buffer
        for i in range(self.size):
            gridˣ[i] = 0

    # Method for nullifying the Δ buffer
    @cython.pheader(# Locals
                    i='Py_ssize_t',
                    Δ='double*',
                    )
    def nullify_Δ(self):
        # Extract Δ buffer pointer
        Δ = self.Δ
        # Nullify Δ buffer
        for i in range(self.size):
            Δ[i] = 0

    # Method for copying the content of grid into gridˣ
    @cython.pheader(
        # Arguments
        operation=str,
        # Locals
        i='Py_ssize_t',
        grid='double*',
        gridˣ='double*',
    )
    def copy_grid_to_gridˣ(self, operation='='):
        grid, gridˣ = self.grid, self.gridˣ
        for i in range(self.size):
            with unswitch:
                if operation == '=':
                    gridˣ[i] = grid[i]
                elif operation == '+=':
                    gridˣ[i] += grid[i]

    # Method for copying the content of gridˣ into grid
    @cython.pheader(
        # Arguments
        operation=str,
        # Locals
        i='Py_ssize_t',
        grid='double*',
        gridˣ='double*',
    )
    def copy_gridˣ_to_grid(self, operation='='):
        grid, gridˣ = self.grid, self.gridˣ
        for i in range(self.size):
            with unswitch:
                if operation == '=':
                    grid[i] = gridˣ[i]
                elif operation == '+=':
                    grid[i] += gridˣ[i]

    # This method is automaticlly called when a FluidScalar instance
    # is garbage collected. All manually allocated memory is freed.
    def __dealloc__(self):
        free(self.grid)
        if not self.is_linear:
            free(self.gridˣ)

    # String representation
    def __repr__(self):
        return '<fluidscalar {}[{}]>'.format(self.varnum,
                                             ', '.join([str(mi) for mi in self.multi_index]))
    def __str__(self):
        return self.__repr__()



# Class providing the data structure for particle tiling.
# Though the variable naming only refer to tiles (within a domain),
# subtiles (within a tile) makes use of this data structure as well.
@cython.cclass
class Tiling:
    # Initialization method
    @cython.header(
        # Arguments
        tiling_name=str,
        component='Component',
        shape=object,  # sequence of length 3 or int-like
        extent='double[::1]',
        initial_rung_size=object,  # sequence of length N_rungs or int-like
        refinement_period='Py_ssize_t',
        # Locals
        dim='int',
        i='Py_ssize_t',
        initial_rung_sizes='Py_ssize_t[::1]',
        j='Py_ssize_t',
        k='Py_ssize_t',
        rung_index='signed char',
        tile='Py_ssize_t**',
        tile_index='Py_ssize_t',
        tile_index3D='Py_ssize_t[::1]',
        rung='Py_ssize_t*',
        rungs_sizes='Py_ssize_t*',
        rungs_N='Py_ssize_t*',
    )
    def __init__(self, tiling_name, component, shape, extent,
        initial_rung_size, refinement_period):
        # The triple quoted string below serves as the type declaration
        # for the data attributes of the Tiling type.
        # It will get picked up by the pyxpp script
        # and indluded in the .pxd file.
        """
        str                   name
        bint                  is_trivial
        Component             component
        Py_ssize_t[::1]       shape
        Py_ssize_t            size
        Py_ssize_t[:, :, ::1] layout
        Py_ssize_t[:, ::1]    layout_1Dto3D
        Py_ssize_t***         tiles
        Py_ssize_t**          tiles_rungs_N
        Py_ssize_t**          tiles_rungs_sizes
        signed char*          contain_particles
        double[::1]           location
        double[::1]           extent
        double[::1]           tile_extent
        Py_ssize_t            refinement_period
        Py_ssize_t            refinement_offset
        double                computation_time
        double                computation_time_total
        """
        # Remember the name of this tiling
        self.name = tiling_name
        # The tiling with the name 'trivial' is special.
        # Note whether this tiling is the trivial tiling.
        self.is_trivial = (self.name == 'trivial')
        # A separate Tiling instance should be used for each component
        self.component = component
        # The shape of this tiling.
        # If a single number is provided, a cubic tiling is created.
        shape = any2list(shape)
        if len(shape) == 1:
            shape *= 3
        elif len(shape) != 3:
            abort(f'Tilings need a 3D shape, but shape = {shape} is given')
        shape = asarray(shape, dtype=C2np['Py_ssize_t'])
        if np.any(shape < 1):
            abort(
                f'Tilings must have a size of at least 1 in each dimension, '
                f'but a tiling of shape {shape} is about to be created.'
            )
        self.shape = shape
        self.size = np.prod(self.shape)
        # The tiling layout; mapping from 3D integer tile indices
        # to 1D integer tile indices.
        self.layout = arange(self.size, dtype=C2np['Py_ssize_t']
            ).reshape(tuple(self.shape))
        # Mapping from 1D tile indices to 3D tile indices.
        # This is implemented as a 2D array.
        self.layout_1Dto3D = empty((self.size, 3), dtype=C2np['Py_ssize_t'])
        for         i in range(self.shape[0]):
            for     j in range(self.shape[1]):
                for k in range(self.shape[2]):
                    tile_index = self.layout[i, j, k]
                    tile_index3D = self.layout_1Dto3D[tile_index, :]
                    tile_index3D[0] = i
                    tile_index3D[1] = j
                    tile_index3D[2] = k
        # Create array of initial rung sizes. If the passed
        # initial_rung_size is a single number, this will be used for
        # all rungs.
        initial_rung_size = any2list(initial_rung_size)
        if len(initial_rung_size) == 1:
            initial_rung_size *= N_rungs
        elif len(initial_rung_size) != N_rungs:
            abort(
                f'A Tiling with initial rung sizes of {initial_rung_size} is about '
                f'to be initialized, but we need one size for each of the {N_rungs} rungs.'
            )
        initial_rung_sizes = asarray(initial_rung_size, dtype=C2np['Py_ssize_t'])
        # The tiles themselves. This is a triple pointer,
        # indexed in the following way:
        # tile = tiles[tile_index],
        # rung = tile[rung_index],
        # particle_index = rung[rung_particle_index],
        # where particle_index index directly into e.g. Component.posx.
        # The number of tiles is fixed for any given tiling,
        # and the number of rungs is always given by N_rungs.
        # The number of particles within a rung is not constant,
        # however, and so we additionally need to keep track of the
        # allocation and occupation size of each rung.
        # These are denoted rungs_sizes and rungs_N, and are similarly
        # indexed as
        # rungs_sizes = tiles_rungs_sizes[tile_index],
        # rung_size = rungs_sizes[rung_index],
        # and
        # rungs_N = tiles_rungs_N[tile_index],
        # rung_N = rungs_N[rung_index].
        # Thus, rung_particle_index goes from 0 to rung_N - 1.
        # We furthermore have the contain_particles array, which
        # indicate the content of a given tile:
        # contain_particles[tile_index] == 0 -> No particles at all
        # contain_particles[tile_index] == 1 -> Only inactive particles
        # contain_particles[tile_index] == 2 -> Active particles
        self.tiles             = malloc(self.size*sizeof('Py_ssize_t**'))
        self.tiles_rungs_sizes = malloc(self.size*sizeof('Py_ssize_t*'))
        self.tiles_rungs_N     = malloc(self.size*sizeof('Py_ssize_t*'))
        self.contain_particles = malloc(self.size*sizeof('signed char'))
        for tile_index in range(self.size):
            tile = malloc(N_rungs*sizeof('Py_ssize_t*'))
            self.tiles[tile_index] = tile
            for rung_index in range(N_rungs):
                rung = malloc(initial_rung_sizes[rung_index]*sizeof('Py_ssize_t'))
                tile[rung_index] = rung
            rungs_sizes = malloc(N_rungs*sizeof('Py_ssize_t'))
            self.tiles_rungs_sizes[tile_index] = rungs_sizes
            for rung_index in range(N_rungs):
                rungs_sizes[rung_index] = initial_rung_sizes[rung_index]
            rungs_N = malloc(N_rungs*sizeof('Py_ssize_t'))
            self.tiles_rungs_N[tile_index] = rungs_N
            for rung_index in range(N_rungs):
                rungs_N[rung_index] = 0
            self.contain_particles[tile_index] = 0
        # When sorting particles into tiles, we need to know the spatial
        # location of each tile. For this, we need the position of the
        # beginning of the tiling (the left, backward, lower corner of
        # the [0, 0, 0] tile) as well as the size of the complete
        # tiling. While the extent is supplied to this initialiser
        # method, the location should be adjusted afterwards using
        # the relocate method.
        self.extent = extent
        self.tile_extent = empty(3, dtype=C2np['double'])
        for dim in range(3):
            self.tile_extent[dim] = self.extent[dim]/self.shape[dim]
        self.location = zeros(3, dtype=C2np['double'])
        # If this is a subtiling and it uses automatic refinement,
        # it needs a refinement period (measured in base time steps).
        # This is set by the subtiling_refinement_period user parameter.
        # However, this is for subtilings with cubic subtiles, i.e. subtilings
        # where the subtiles have the same physical extent in each
        # direction (not necessarily a shape of the form [n]*3).
        # Such cubic subtiles will get all three dimensions refined
        # simultaneously, and thus stay cubic. For non-cubic
        # subtiles, the refinement works on only one or two
        # directions at a time, and hence these are in need of a
        # shorter refinement period.
        if refinement_period > 0:
            refinement_period = int(round(refinement_period/len(set(self.tile_extent))))
            if refinement_period < subtiling_refinement_period_min:
                refinement_period = subtiling_refinement_period_min
        self.refinement_period = refinement_period
        self.refinement_offset = 0
        # The running total computation time,
        # measured over interactions between tile pairs.
        # Both the computation_time and computation_time_total
        # attributes are set by the interactions module.
        # The main module looks up computation_time_total,
        # and nullifies it at the beginning of each time step.
        self.computation_time = 0
        self.computation_time_total = 0

    # Method for resizing a given rung within a given tile.
    # If no rung_size is given, the size of the rung
    # will be increased by a factor rung_growth.
    @cython.header(
        # Arguments
        tile_index='Py_ssize_t',
        rung_index='signed char',
        rung_size='Py_ssize_t',
        # Locals
        rung='Py_ssize_t*',
        rung_growth='double',
        rungs_sizes='Py_ssize_t*',
        tile='Py_ssize_t**',
        returns='void',
    )
    def resize(self, tile_index, rung_index, rung_size=-1):
        tile        = self.tiles            [tile_index]
        rungs_sizes = self.tiles_rungs_sizes[tile_index]
        rung = tile[rung_index]
        if rung_size == -1:
            rung_growth = 1.1
            rung_size = cast(rung_growth*rungs_sizes[rung_index], 'Py_ssize_t') + 1
        rung = realloc(rung, rung_size*sizeof('Py_ssize_t'))
        tile       [rung_index] = rung
        rungs_sizes[rung_index] = rung_size

    # Method for spatially relocating the tiling
    @cython.header(
        # Arguments
        location='double[::1]',
        # Locals
        returns='void',
    )
    def relocate(self, location):
        self.location = location

    # Method for converting a 1D/linear tile index
    # into its 3D equivalent.
    @cython.header(
        # Arguments
        tile_index='Py_ssize_t',
        # Locals
        tile_index3D='Py_ssize_t[::1]',
        returns='Py_ssize_t[::1]',
    )
    def tile_index3D(self, tile_index):
        tile_index3D = self.layout_1Dto3D[tile_index, :]
        return tile_index3D

    # Method for sorting particles into tiles. If the arguments
    # coarse_tiling and coarse_tiling_index are left out,
    # all particles within the attached component will be
    # tile sorted. Otherwise, only the subset of the particles
    # given by these additional arguments will be taken into account
    # when performing the tile sorting.
    @cython.header(
        # Arguments
        coarse_tiling='Tiling',
        coarse_tile_index='Py_ssize_t',
        # Locals
        coarse_rung='Py_ssize_t*',
        coarse_rung_N='Py_ssize_t',
        coarse_rung_index='signed char',
        coarse_rung_particle_index='Py_ssize_t',
        coarse_rungs_N='Py_ssize_t*',
        coarse_tile='Py_ssize_t**',
        contain_particles='signed char*',
        contains='signed char',
        i='Py_ssize_t',
        j='Py_ssize_t',
        k='Py_ssize_t',
        layout='Py_ssize_t[:, :, ::1]',
        lowest_active_rung='signed char',
        particle_index='Py_ssize_t',
        posx='double*',
        posy='double*',
        posz='double*',
        rung='Py_ssize_t*',
        rung_N='Py_ssize_t',
        rung_index='signed char',
        rung_indices='signed char*',
        rungs_N='Py_ssize_t*',
        tile_index='Py_ssize_t',
        tiles='Py_ssize_t***',
        tiles_rungs_N='Py_ssize_t**',
        tiles_rungs_sizes='Py_ssize_t**',
        returns='void',
    )
    def sort(self, coarse_tiling=None, coarse_tile_index=-1):
        # Extract variables
        layout = self.layout
        rung_indices       = self.component.rung_indices
        posx               = self.component.posx
        posy               = self.component.posy
        posz               = self.component.posz
        lowest_active_rung = self.component.lowest_active_rung
        tiles             = self.tiles
        tiles_rungs_sizes = self.tiles_rungs_sizes
        tiles_rungs_N     = self.tiles_rungs_N
        contain_particles = self.contain_particles
        # If only the particles within a coarser tile should be sorted
        # (into finer tiles), extract this coarse tile.
        if 𝔹[coarse_tiling is not None]:
            # If this is the trivial tiling, the only allowed coarse
            # tiling is itself. It is thus already sorted.
            if self.is_trivial:
                return
            coarse_tile = coarse_tiling.tiles[coarse_tile_index]
            coarse_rungs_N = coarse_tiling.tiles_rungs_N[coarse_tile_index]
        # Reset the particle count for each rung within every tile
        for tile_index in range(self.size):
            rungs_N = tiles_rungs_N[tile_index]
            for rung_index in range(N_rungs):
                rungs_N[rung_index] = 0
        # Reset particle content within each tile
        for tile_index in range(self.size):
            contain_particles[tile_index] = 0
        # Place each particle into a tile. If any of the particles
        # are outside of the tiling, this will fail.
        coarse_rung_N = 1  # Needed when not using a coarse tile
        rung_index = 0     # Needed when not using rungs
        tile_index = 0     # Used if this is the trivial tiling
        for particle_index in range(self.component.N_local if coarse_tiling is None else N_rungs):
            # When a coarse tile is in use, the particle_index
            # variable is really the rung_index for the coarse tile.
            # Use it to pick out the coarse rung.
            with unswitch:
                if 𝔹[coarse_tiling is not None]:
                    coarse_rung_index = particle_index
                    coarse_rung   = coarse_tile   [coarse_rung_index]
                    coarse_rung_N = coarse_rungs_N[coarse_rung_index]
            # Loop over the particles in this coarse rung. When not
            # using a coarse tile, this is a one-iteration loop.
            for coarse_rung_particle_index in range(coarse_rung_N):
                with unswitch:
                    if 𝔹[coarse_tiling is not None]:
                        particle_index = coarse_rung[coarse_rung_particle_index]
                # Determine the tile within which this particle
                # is located. For the trivial tiling,
                # we already know the answer.
                with unswitch:
                    if not self.is_trivial:
                        i = cast((posx[particle_index] - ℝ[self.location[0]])
                            *ℝ[1/self.tile_extent[0]], 'Py_ssize_t')
                        j = cast((posy[particle_index] - ℝ[self.location[1]])
                            *ℝ[1/self.tile_extent[1]], 'Py_ssize_t')
                        k = cast((posz[particle_index] - ℝ[self.location[2]])
                            *ℝ[1/self.tile_extent[2]], 'Py_ssize_t')
                        tile_index = layout[i, j, k]
                # Get the index of the rung for this particle
                with unswitch:
                    if self.component.use_rungs:
                        rung_index = rung_indices[particle_index]
                # Resize this rung within the tile, if needed
                rungs_N = tiles_rungs_N[tile_index]
                rung_N = rungs_N[rung_index]
                if rung_N == tiles_rungs_sizes[tile_index][rung_index]:
                    self.resize(tile_index, rung_index)
                # Add this particle to the rung within the tile
                rung = tiles[tile_index][rung_index]
                rung[rung_N] = particle_index
                rungs_N[rung_index] += 1
                # Update tile content
                if rung_index < lowest_active_rung:
                    # This particle sits on an inactive rung
                    contains = 1
                else:
                    # This particle sits on an active rung
                    contains = 2
                if contain_particles[tile_index] < contains:
                    contain_particles[tile_index] = contains
    # This method is automaticlly called when a Tiling instance
    # is garbage collected. All manually allocated memory is freed.
    def __dealloc__(self):
        cython.declare(
            rung_index='signed char',
            tile='Py_ssize_t**',
            tile_index='Py_ssize_t',
        )
        for tile_index in range(self.size):
            tile = self.tiles[tile_index]
            for rung_index in range(N_rungs):
                free(tile[rung_index])
            free(tile)
            free(self.tiles_rungs_sizes[tile_index])
            free(self.tiles_rungs_N[tile_index])
        free(self.tiles)
        free(self.tiles_rungs_sizes)
        free(self.tiles_rungs_N)
        free(self.contain_particles)

    # String representation
    def __repr__(self):
        return f'<tiling of component "{self.component.name}" with shape {tuple(self.shape)}>'
    def __str__(self):
        return self.__repr__()


# The class governing any component of the universe
@cython.cclass
class Component:
    """An instance of this class represents either a collection of
    particles or a grid of fluid values. A Component instance should be
    present on all processes.
    """

    # Initialization method
    @cython.pheader(
        # Arguments
        name=str,
        species=str,
        N_or_gridsize='Py_ssize_t',
        mass='double',
        boltzmann_order='Py_ssize_t',
        forces=dict,
        class_species=object,  # str or container of str's
        w=object,  # NoneType, float, int, str or dict
        boltzmann_closure=str,
        approximations=dict,
        softening_length=object,  # float or str
        realization_options=dict,
        # Locals
        i='Py_ssize_t',
    )
    def __init__(self, name, species, N_or_gridsize, *,
        mass=-1,
        boltzmann_order=-2,
        forces=None,
        class_species=None,
        realization_options=None,
        w=None,
        boltzmann_closure=None,
        approximations=None,
        softening_length=None,
    ):
        # The keyword-only arguments are passed from dicts in the
        # initial_conditions user parameter. If not specified there
        # (None passed) they will be set trough other parameters.
        # Of special interest is the fluid parameters boltzmann_order,
        # boltzmann_closure and approximations. Together, these control
        # the degree to which a fluid component will behave non-
        # linearly. Below is listed an overview of all allowed
        # combinations of boltzmann_order and boltzmann_closure,
        # together with the accompanying fluid variable behavoir.
        # Note that for particle components, only boltzmann_order = 1
        # is allowed.
        #
        # boltzmann_order = -1, boltzmann_closure = 'class':
        #     linear ϱ  (Realized continuously, affects other components gravitationally)
        #
        # boltzmann_order = 0, boltzmann_closure = 'truncate':
        #     non-linear ϱ  (Though "non-linear", ϱ is frozen in time as no J exist.
        #                    Also, unlike when boltzmann_order = -1 and
        #                    boltzmann_closure = 'class', ϱ will only be realized
        #                    at the beginning of the simulation.)
        #
        # boltzmann_order = 0, boltzmann_closure = 'class':
        #     non-linear ϱ
        #         linear J  (realized continuously)
        #         linear 𝒫  (P=wρ approximation enforced)
        #
        # boltzmann_order: 1, boltzmann_closure = 'truncate':
        #     non-linear ϱ
        #     non-linear J
        #         linear 𝒫  (P=wρ approximation enforced)
        #
        # boltzmann_order = 1, boltzmann_closure = 'class':
        #     non-linear ϱ
        #     non-linear J
        #         linear 𝒫  (realized continuously)
        #         linear ς  (realized continuously)
        #
        # boltzmann_order = 2, boltzmann_closure = 'truncate':
        #     non-linear ϱ
        #     non-linear J
        #     non-linear 𝒫  (Though "non-linear", 𝒫 is frozen in time since the evolution equation
        #                    for 𝒫 is not implemented.
        #                    Also, unlike when boltzmann_order = 1 and boltzmann_closure = 'class',
        #                    𝒫 will only be realized at the beginning of the simulation.)
        #     non-linear ς  (Though "non-linear", ς is frozen in time since the evolution equation
        #                    for ς is not implemented.
        #                    Also, unlike when boltzmann_order = 1 and boltzmann_closure = 'class',
        #                    ς will only be realized at the beginning of the simulation.)
        #
        # The triple quoted string below serves as the type declaration
        # for the data attributes of the Component type.
        # It will get picked up by the pyxpp script
        # and indluded in the .pxd file.
        """
        # General component attributes
        public str name
        public str species
        public str representation
        public dict forces
        public str class_species
        # Particle attributes
        public Py_ssize_t N
        public Py_ssize_t N_allocated
        public Py_ssize_t N_local
        public double mass
        public double softening_length
        # Particle data
        double* posx
        double* posy
        double* posz
        double* momx
        double* momy
        double* momz
        double** pos
        double** mom
        public double[::1] posx_mv
        public double[::1] posy_mv
        public double[::1] posz_mv
        public double[::1] momx_mv
        public double[::1] momy_mv
        public double[::1] momz_mv
        public list pos_mv
        public list mom_mv
        # Particle Δ buffers
        double* Δposx
        double* Δposy
        double* Δposz
        double* Δmomx
        double* Δmomy
        double* Δmomz
        double** Δpos
        double** Δmom
        public double[::1] Δposx_mv
        public double[::1] Δposy_mv
        public double[::1] Δposz_mv
        public double[::1] Δmomx_mv
        public double[::1] Δmomy_mv
        public double[::1] Δmomz_mv
        public list Δpos_mv
        public list Δmom_mv
        # Short-range rungs
        bint use_rungs
        signed char lowest_active_rung
        signed char lowest_populated_rung
        signed char highest_populated_rung
        Py_ssize_t* rungs_N
        signed char* rung_indices
        signed char[::1] rung_indices_mv
        signed char* rung_jumps
        signed char[::1] rung_jumps_mv
        # Dict used for storing Tiling instances
        public dict tilings
        # Fluid attributes
        public Py_ssize_t gridsize
        public tuple shape
        public tuple shape_noghosts
        public Py_ssize_t size
        public Py_ssize_t size_noghosts
        public dict fluid_names
        public Py_ssize_t boltzmann_order
        public str boltzmann_closure
        public dict realization_options
        public str w_type
        public double w_constant
        public double[:, ::1] w_tabulated
        public str w_expression
        Spline w_spline
        public str w_eff_type
        Spline w_eff_spline
        public dict approximations
        public double _ϱ_bar
        # Fluid data
        public list fluidvars
        FluidScalar ϱ
        public object J  # Tensor
        FluidScalar Jx
        FluidScalar Jy
        FluidScalar Jz
        public object ς  # Tensor
        FluidScalar ςxx
        FluidScalar ςxy
        FluidScalar ςxz
        FluidScalar ςyx
        FluidScalar ςyy
        FluidScalar ςyz
        FluidScalar ςzx
        FluidScalar ςzy
        FluidScalar ςzz
        FluidScalar 𝒫
        """
        # A reference to each and every Component instance ever created
        # is stored in the global components_all set.
        components_all.add(self)
        # Check that the name does not conflict with
        # one of the special names used internally,
        # and that the name has not already been used.
        if name in internally_defined_names:
            masterwarn(
                f'A species by the name of "{name}" is to be created. '
                f'As this name is used internally by the code, '
                f'this may lead to erroneous behaviour.'
            )
        elif not allow_similarly_named_components and name in component_names:
            masterwarn(
                f'A component with the name of "{name}" has already '
                f'been instantiated. Instantiating multiple components '
                f'with the same name may lead to undesired behaviour.'
            )
        for char in ',{}':
            if char in name:
                masterwarn(
                    f'A species by the name of "{name}" is to be created. '
                    f'As this name contains a "{char}" character, '
                    f'this may lead to erroneous behaviour.'
                )
        if name:
            component_names.add(name)
        # General attributes
        self.name    = name
        self.species = species
        self.representation = get_representation(self.species)
        if self.representation == 'particles':
            self.N = N_or_gridsize
            self.gridsize = 1
        elif self.representation == 'fluid':
            self.gridsize = N_or_gridsize
            self.N = 1
            if self.gridsize%2 != 0:
                masterwarn(
                    f'The fluid component "{self.name}" has an odd gridsize ({self.gridsize}). '
                    f'Some operations may not function correctly.'
                )
        # Set forces (and force methods)
        if forces is None:
            forces = is_selected(self, select_forces, accumulate=True)
        if not forces:
            forces = {}
        self.forces = {}
        for force, method in forces.items():
            force, method = force.lower(), method.lower()
            for char in ' _-^()':
                force, method = force.replace(char, ''), method.replace(char, '')
            for n in range(10):
                force  =  force.replace(unicode_superscript(str(n)), str(n))
                method = method.replace(unicode_superscript(str(n)), str(n))
            self.forces[force] = method
        # Set the CLASS species
        if class_species is None:
            class_species = is_selected(self, select_class_species)
        elif not isinstance(class_species, str):
            class_species = '+'.join(class_species)
        if class_species == 'default':
            if self.species in default_class_species:
                class_species = default_class_species[self.species]
            else:
                abort(
                    f'Default CLASS species assignment failed because '
                    f'the species "{self.species}" does not map to any CLASS species'
                )
        self.class_species = class_species
        # Set closure rule for the Boltzmann hierarchy
        if boltzmann_closure is None:
            boltzmann_closure = is_selected(self, select_boltzmann_closure)
        if not boltzmann_closure:
            boltzmann_closure = ''
        self.boltzmann_closure = boltzmann_closure.lower()
        if self.representation == 'fluid' and self.boltzmann_closure not in ('truncate', 'class'):
            abort(
                f'The component "{self.name}" was initialized '
                f'with an unknown Boltzmann closure of "{self.boltzmann_closure}"'
            )
        # Set realization options
        if realization_options is None:
            realization_options = {}
        realization_options_selected = is_selected(
            self, select_realization_options, accumulate=True)
        realization_options_selected.update(realization_options)
        realization_options = realization_options_selected
        realization_options_all = realization_options.get('all', {})
        for key, val in realization_options.copy().items():
            if not isinstance(val, dict):
                realization_options_all[key] = val
                del realization_options[key]
        realization_options_all = {
            key.lower().replace(' ', '').replace('-', '').replace('_', ''):
                (val.lower().replace(' ', '').replace('-', '').replace('_', '') if
                isinstance(val, str) else val)
            for key, val in realization_options_all.items()}
        varnames = {
            'particles': ('pos', 'mom'),
            'fluid': ('ϱ', 'J', '𝒫', 'ς'),
        }[self.representation]
        wrong_varname_sets = {
            'pos': {'x', 'position', 'positions', 'Position', 'Positions'},
            'mom': {'momentum', 'momenta', 'Momentum', 'Momenta'},
            'ϱ': {'r', 'rho', 'ρ'},
            'J': {'j'},
            '𝒫': {'P', 'δP', 'δ𝒫', 'p', 'δp'},
            'ς': {'s', 'sigma', 'Sigma', 'σ', 'Σ'},
        }
        for varname in varnames:
            wrong_varnames = wrong_varname_sets[varname]
            for wrong_varname in wrong_varnames:
                realization_options_varname = (
                       realization_options.get(unicode(wrong_varname))
                    or realization_options.get(asciify(wrong_varname))
                )
                if realization_options_varname:
                    realization_options[varname] = realization_options_varname
                    break
        realization_options_default = {
            # Linear realization options
            'velocitiesfromdisplacements': realization_options_all.get(
                'velocitiesfromdisplacements', False),
            'backscaling': realization_options_all.get('backscaling', False),
            # Non-linear realization options
            'structure'    : realization_options_all.get('structure', 'nonlinear'),
            'compoundorder': realization_options_all.get('compoundorder', 'linear'),
        }
        for varname in varnames:
            realization_options_default_copy = realization_options_default.copy()
            for varname_encoding in (unicode(varname), asciify(varname)):
                if varname_encoding in realization_options:
                    realization_options_default_copy.update(realization_options[varname_encoding])
            realization_options[varname] = realization_options_default_copy
        for varname, realization_options_varname in realization_options.copy().items():
            realization_options[unicode(varname)] = realization_options_varname.copy()
            realization_options[asciify(varname)] = realization_options_varname.copy()
        realization_options = {
            varname: {
                key.lower().replace(' ', '').replace('-', '').replace('_', ''):
                    (val.lower().replace(' ', '').replace('-', '').replace('_', '') if
                    isinstance(val, str) else val)
                for key, val in realization_options[varname].items()}
            for varname in varnames
        }
        for varname, realization_options_varname in realization_options.copy().items():
            realization_options[unicode(varname)] = realization_options_varname.copy()
            realization_options[asciify(varname)] = realization_options_varname.copy()
            for realization_option_varname in realization_options_varname:
                if realization_option_varname not in {
                    # Linear realization options
                    'velocitiesfromdisplacements',
                    'backscaling',
                    # Non-linear realization options
                    'structure',
                    'compoundorder',
                }:
                    abort(
                        f'Realization option "{realization_option_varname}" (specified for '
                        f'component "{self.name}" not recognized.'
                    )
        if self.representation == 'particles':
            # None of the non-linear relization options
            # makes sense for particle components.
            for realization_options_varname in realization_options.values():
                del realization_options_varname['structure']
                del realization_options_varname['compoundorder']
        elif self.representation == 'fluid':
            # None of the non-linear relization options
            # makes sense for ϱ.
            for realization_options_varname in (
                realization_options[unicode('ϱ')],
                realization_options[asciify('ϱ')],
            ):
                del realization_options_varname['structure']
                del realization_options_varname['compoundorder']
        for varname, realization_options_varname in realization_options.items():
            if varname != 'mom':
                if realization_options_varname['velocitiesfromdisplacements']:
                    masterwarn(
                        f'The "velocities from displacements" realization option was set to True '
                        f'for the "{varname}" variable of the "{self.name}" component, but in only '
                        f'makes sense for the "mom" variable'
                        + ('' if self.representation == 'particles' else
                            ' (and only for particle components)')
                    )
                del realization_options_varname['velocitiesfromdisplacements']
        self.realization_options = realization_options
        # Set approximations. Ensure that all implemented approximations
        # get set to either True or False. If an approximation is not
        # set for this component, its value defaults to False.
        # Also, specific circumstances may force some approximations to
        # have a specific value.
        if approximations is None:
            approximations = is_selected(self, select_approximations, accumulate=True)
        if not approximations:
            approximations = {}
        approximations_transformed = {}
        for approximation, value in approximations.items():
            # General transformations
            approximation = unicode(approximation)
            for char in unicode(' *×^'):
                approximation = approximation.replace(char, '')
            for n in range(10):
                approximation = approximation.replace(unicode_superscript(str(n)), str(n))
            # The P=wρ approximation
            approximation_transformed = approximation
            for s in ('\rho', r'\rho', 'rho'):
                approximation_transformed = approximation_transformed.replace(s, unicode('ρ'))
            if approximation_transformed in {
                    unicode('P=wρ'),
                    unicode('P=ρw'),
                    unicode('wρ=P'),
                    unicode('ρw=P'),
            }:
                approximation_transformed = unicode('P=wρ')
            approximations_transformed[approximation_transformed] = bool(value)
        approximations = approximations_transformed
        for approximation, value in approximations.copy().items():
            if unicode(approximation) not in approximations_implemented:
                abort(
                    f'The component "{self.name}" was initialized '
                    f'with the unknown approximation "{approximation}"'
                )
            approximations[asciify(approximation)] = value
            approximations[unicode(approximation)] = value
        for approximation in approximations_implemented:
            value = approximations.get(approximation, False)
            approximations[asciify(approximation)] = value
            approximations[unicode(approximation)] = value
        if self.representation == 'particles':
            approximations[unicode('P=wρ')] = True
            approximations[asciify('P=wρ')] = True
        self.approximations = approximations
        # Set softening length
        if softening_length is None:
            softening_length = is_selected(self, select_softening_length)
        if isinstance(softening_length, str):
            # Evaluate softening_length if it's a str.
            # Replace 'N' with the number of particles of this component
            # and 'gridsize' with the gridsize of this component.
            if self.representation == 'particles':
                softening_length = softening_length.replace('N', str(self.N))
                softening_length = softening_length.replace('gridsize', str(cbrt(self.N)))
            elif self.representation == 'fluid':
                softening_length = softening_length.replace('N', str(self.gridsize**3))
                softening_length = softening_length.replace('gridsize', str(self.gridsize))
            softening_length = eval(softening_length, globals(), units_dict)
        if not softening_length:
            if self.representation == 'particles':
                # If no name is given, this is an internally
                # used component, in which case it is OK not to have
                # any softening lenth set.
                if self.name:
                    masterwarn(f'No softening length set for particles component "{self.name}"')
            softening_length = 0
        self.softening_length = float(softening_length)
        # This attribute will store the conserved mean density
        # of this component. It is set by the ϱ_bar method.
        self._ϱ_bar = -1
        # Particle attributes
        self.mass = mass
        self.N_allocated = 1
        self.N_local = 1
        # Particle data
        self.posx = malloc(self.N_allocated*sizeof('double'))
        self.posy = malloc(self.N_allocated*sizeof('double'))
        self.posz = malloc(self.N_allocated*sizeof('double'))
        self.momx = malloc(self.N_allocated*sizeof('double'))
        self.momy = malloc(self.N_allocated*sizeof('double'))
        self.momz = malloc(self.N_allocated*sizeof('double'))
        self.posx_mv = cast(self.posx, 'double[:self.N_allocated]')
        self.posy_mv = cast(self.posy, 'double[:self.N_allocated]')
        self.posz_mv = cast(self.posz, 'double[:self.N_allocated]')
        self.momx_mv = cast(self.momx, 'double[:self.N_allocated]')
        self.momy_mv = cast(self.momy, 'double[:self.N_allocated]')
        self.momz_mv = cast(self.momz, 'double[:self.N_allocated]')
        # Pack particle data into a pointer array of pointers
        # and a list of memoryviews.
        self.pos = malloc(3*sizeof('double*'))
        self.pos[0] = self.posx
        self.pos[1] = self.posy
        self.pos[2] = self.posz
        self.mom = malloc(3*sizeof('double*'))
        self.mom[0] = self.momx
        self.mom[1] = self.momy
        self.mom[2] = self.momz
        self.pos_mv = [self.posx_mv, self.posy_mv, self.posz_mv]
        self.mom_mv = [self.momx_mv, self.momy_mv, self.momz_mv]
        # Particle data buffers
        self.Δposx = malloc(self.N_allocated*sizeof('double'))
        self.Δposy = malloc(self.N_allocated*sizeof('double'))
        self.Δposz = malloc(self.N_allocated*sizeof('double'))
        self.Δmomx = malloc(self.N_allocated*sizeof('double'))
        self.Δmomy = malloc(self.N_allocated*sizeof('double'))
        self.Δmomz = malloc(self.N_allocated*sizeof('double'))
        self.Δposx_mv = cast(self.Δposx, 'double[:self.N_allocated]')
        self.Δposy_mv = cast(self.Δposy, 'double[:self.N_allocated]')
        self.Δposz_mv = cast(self.Δposz, 'double[:self.N_allocated]')
        self.Δmomx_mv = cast(self.Δmomx, 'double[:self.N_allocated]')
        self.Δmomy_mv = cast(self.Δmomy, 'double[:self.N_allocated]')
        self.Δmomz_mv = cast(self.Δmomz, 'double[:self.N_allocated]')
        # Pack particle data buffers into a pointer array of pointers
        # and a list of memoryviews.
        self.Δpos = malloc(3*sizeof('double*'))
        self.Δpos[0] = self.Δposx
        self.Δpos[1] = self.Δposy
        self.Δpos[2] = self.Δposz
        self.Δmom = malloc(3*sizeof('double*'))
        self.Δmom[0] = self.Δmomx
        self.Δmom[1] = self.Δmomy
        self.Δmom[2] = self.Δmomz
        self.Δpos_mv = [self.Δposx_mv, self.Δposy_mv, self.Δposz_mv]
        self.Δmom_mv = [self.Δmomx_mv, self.Δmomy_mv, self.Δmomz_mv]
        # Short-range rungs
        self.use_rungs = bool(
            N_rungs > 1
            and self.representation == 'particles'
            and ({'pp', 'p3m'} & set(self.forces.values()))
        )
        self.lowest_active_rung = 0
        self.lowest_populated_rung = 0
        self.highest_populated_rung = 0
        self.rungs_N = malloc(N_rungs*sizeof('Py_ssize_t'))
        for i in range(N_rungs):
            self.rungs_N[i] = 0
        self.rung_indices = malloc(self.N_local*sizeof('signed char'))
        self.rung_indices_mv = cast(self.rung_indices, 'signed char[:self.N_local]')
        for i in range(self.N_local):
            self.rung_indices[i] = 0
        self.rung_jumps = malloc(self.N_local*sizeof('signed char'))
        for i in range(self.N_local):
            self.rung_jumps[i] = 0
        self.rung_jumps_mv = cast(self.rung_jumps, 'signed char[:self.N_local]')
        # Dict used for storing Tiling instances
        self.tilings = {}
        # Fluid attributes
        if boltzmann_order == -2:
            boltzmann_order = is_selected(self, select_boltzmann_order)
        self.boltzmann_order = boltzmann_order
        if self.representation == 'particles':
            if self.boltzmann_order != 1:
                abort(
                    f'Particle components must have boltzmann_order = 1, '
                    f'but boltzmann_order = {self.boltzmann_order} was specified for "{self.name}"'
                )
        elif self.representation == 'fluid':
            if self.boltzmann_order < -1:
                abort(
                    f'Having boltzmann_order < -1 are nonsensical, '
                    f'but boltzmann_order = {self.boltzmann_order} was specified for "{self.name}"'
                )
            if self.boltzmann_order == -1 and self.boltzmann_closure == 'truncate':
                abort(
                    f'The fluid component "{self.name}" has no non-linear and no '
                    f'linear fluid variables, and so practically it does not exist. '
                    f'Such components are disallowed.'
                )
            if self.boltzmann_order == 2 and self.boltzmann_closure == 'class':
                abort(
                    f'The "{self.name}" component wants to close the Boltzmann hierarchy using '
                    f'the linear variable after ς from CLASS, which is not implemented. '
                    f'You need to lower its Boltzmann order to '
                    f'1 or use boltzmann_closure = "truncate".'
                )
            if self.boltzmann_order > 2:
                abort(
                    f'Fluids with boltzmann_order > 2 are not implemented, '
                    f'but boltzmann_order = {self.boltzmann_order} was specified for "{self.name}"'
                )
        self.shape = (1, 1, 1)
        self.shape_noghosts = (1, 1, 1)
        self.size = np.prod(self.shape)
        self.size_noghosts = np.prod(self.shape_noghosts)
        # Components with Boltzmann order -1 cannot received forces but
        # may still take part in interactions as suppliers. Any such
        # component should not specify a particul method for its forces.
        if self.boltzmann_order == -1:
            self.forces = {key: '' for key in self.forces}
        # Implement species specific restrictions on the forces below
        if self.species == 'lapse':
            # Only allow a component of the special "lapse" species to
            # participate in the lapse interaction.
            self.forces = {
                force: method for force, method in self.forces.items() if force == 'lapse'
            }
        # Fluid components may only receive a force using the PM method
        if self.representation == 'fluid' and master:
            for force, method in self.forces.items():
                if method not in {'pm', ''}:
                    abort(
                        f'Component "{self.name}" wants to receive the {force} force '
                        f'using the {method} method, but only the pm method is allowed '
                        f'for fluid components.'
                    )
        # Set the equation of state parameter w
        if w is None:
            w = is_selected(self, select_eos_w)
        self.initialize_w(w)
        self.initialize_w_eff()
        # Fluid data.
        # Create the (boltzmann_order + 1) non-linear fluid variables
        # and store them in the fluidvars list. This is done even for
        # particle components, as the fluidscalars are all instantiated
        # with a gridsize of 1. The is_linear argument specifies whether
        # the FluidScalar will be a linear or non-linear variable,
        # where a non-linear variable is one that is updated non-
        # linearly, as opposed to a linear variable which is only
        # updated through continuous realization. Currently, only ϱ and
        # J is implemented as non-linear variables. It is still allowed
        # to have boltzmann_order == 2, in which case ς (and 𝒫) is also
        # specified as being non-linear, although no non-linear
        # evolution is implemented, meaning that these will then be
        # constant in time. Note that the 𝒫 fuid variable is
        # treated specially, as it really lives on the same tensor as
        # the ς fluid scalars. Therefore, the 𝒫 fluid scalar
        # is added later.
        self.fluidvars = []
        for i in range(self.boltzmann_order + 1):
            # Instantiate the i'th fluid variable
            # as a 3×3×...×3 (i times) symmetric tensor.
            fluidvar = Tensor(self, i, (3, )*i, symmetric=True)
            # Populate the tensor with fluid scalar fields
            for multi_index in fluidvar.multi_indices:
                fluidvar[multi_index] = FluidScalar(i, multi_index, is_linear=False)
            # Add the fluid variable to the list
            self.fluidvars.append(fluidvar)
        # If CLASS should be used to close the Boltzmann hierarchy,
        # we need one additional fluid variable. This should act like
        # a symmetric tensor of rank boltzmann_order, but really only a
        # single element of this tensor need to exist in memory.
        # For boltzmann_order == 1, ς is the additional fluid variable.
        # Instantiate the scalar element but disguised as a
        # 3×3×...×3 ((boltzmann_order + 1) times) symmetric tensor.
        # Importantly, this fluid variabe is always considered linear.
        if self.boltzmann_closure == 'class':
            disguised_scalar = Tensor(
                self,
                self.boltzmann_order + 1,
                (3, )*(self.boltzmann_order + 1),
                symmetric=True,
            )
            # Populate the tensor with a fluidscalar
            multi_index = disguised_scalar.multi_indices[0]
            disguised_scalar[multi_index] = FluidScalar(
                self.boltzmann_order + 1, multi_index, is_linear=True,
            )
            # Add this additional fluid variable to the list
            self.fluidvars.append(disguised_scalar)
        # Ensure that the approximation P=wρ is set to True
        # for fluid components which have either a linear J
        # fluid variable, or a non-linear J fluid variable but with the
        # non-linear Boltzmann hierarchy truncated right after J.
        if not self.approximations['P=wρ']:
            if (   self.boltzmann_order < 0
                or (self.boltzmann_order == 0 and self.boltzmann_closure == 'truncate')):
                # The 𝒫 fluid scalar does not exist at all for
                # this component, and so whether the P=wρ approximation
                # is True or not does not make much sense.
                # We set it to True, reflecting the fact that
                # 𝒫 certainly is not a non-linear variable.
                self.approximations[asciify('P=wρ')] = True
                self.approximations[unicode('P=wρ')] = True
            elif self.boltzmann_order == 0 and self.boltzmann_closure == 'class':
                masterwarn(
                    f'The P=wρ approximation has been switched on for the "{self.name}" component '
                    f'because Jⁱ = a⁴(ρ + c⁻²P)uⁱ is a linear fluid variable.'
                )
                self.approximations[asciify('P=wρ')] = True
                self.approximations[unicode('P=wρ')] = True
            elif self.boltzmann_order == 1 and self.boltzmann_closure == 'truncate':
                masterwarn(
                    f'The P=wρ approximation has been switched on for the "{self.name}" component '
                    f'because the non-linear Boltzmann hierarchy is truncated after the '
                    f'non-linear fluid variable Jⁱ, while 𝒫 is part of the next fluid variable.'
                )
                self.approximations[asciify('P=wρ')] = True
                self.approximations[unicode('P=wρ')] = True
        # When the P=wρ approximation is False, the fluid variable 𝒫
        # has to follow the structure of ϱ closely. Otherwise, spurious
        # features will develop in ϱ. Display a warning if the
        # realization option for 𝒫 is set so that its structure does not
        # match that of ϱ.
        if not self.approximations['P=wρ']:
            if self.realization_options['𝒫']['structure'] == 'primordial':
                masterwarn(
                    f'It is specified that the 𝒫 fluid variable of the "{self.name}" component '
                    f'should be realized using the primordial structure throughout time. '
                    f'It is known that this generates spurious features.'
                )
        # When the P=wρ approximation is True, the 𝒫 fluid variable is
        # superfluous. Yet, as it is used in the definition of J,
        # J = a⁴(ρ + P)u, P = a**(-3*(1 + w_eff))*𝒫, it is simplest to
        # just always instantiate a complete 𝒫 fluid variable,
        # regardless of whether 𝒫 appears in the closed
        # Boltzmann hierarchy. We place 𝒫 on ς, since 𝒫 is the trace
        # missing from ς. The only time we do not instantiate 𝒫 is for
        # a fluid without any J variable, be it linear or non-linear.
        if not (    self.boltzmann_order < 0
                or (self.boltzmann_order == 0 and self.boltzmann_closure == 'truncate')):
            # We need a 𝒫 fluid scalar
            if (   (self.boltzmann_order == 0 and self.boltzmann_closure == 'class')
                or (self.boltzmann_order == 1 and self.boltzmann_closure == 'truncate')
                ):
                # The ς tensor on which 𝒫 lives does not yet exist.
                # Instantiate a fake ς tensor, used only to store 𝒫.
                self.fluidvars.append(Tensor(self, 2, (), symmetric=True, active=False))
            # Add the 𝒫 fluid scalar to the ς tensor
            self.fluidvars[2]['trace'] = FluidScalar(0, 0,
                is_linear=(self.boltzmann_order < 2 or self.approximations['P=wρ']),
            )
        # Construct mapping from names of fluid variables (e.g. J)
        # to their indices in self.fluidvars, and also from names of
        # fluid scalars (e.g. ϱ, Jx) to tuple of the form
        # (index, multi_index). The fluid scalar is then given
        # by self.fluidvars[index][multi_index].
        # Also include trivial mappings from indices to themselves,
        # and the special "reverse" mapping from indices to names
        # given by the 'ordered' key.
        self.fluid_names = {'ordered': fluidvar_names[:
                self.boltzmann_order + (1 if self.boltzmann_closure == 'truncate' else 2)
            ]
        }
        for index, (fluidvar, fluidvar_name) in enumerate(
            zip(self.fluidvars, self.fluid_names['ordered'])
        ):
            # The fluid variable
            self.fluid_names[asciify(fluidvar_name)] = index
            self.fluid_names[unicode(fluidvar_name)] = index
            self.fluid_names[index                 ] = index
            # The fluid scalars
            for multi_index in fluidvar.multi_indices:
                fluidscalar_name = fluidvar_name
                if index > 0:
                    fluidscalar_name += ''.join(['xyz'[mi] for mi in multi_index])
                self.fluid_names[asciify(fluidscalar_name)] = (index, multi_index)
                self.fluid_names[unicode(fluidscalar_name)] = (index, multi_index)
                self.fluid_names[index, multi_index       ] = (index, multi_index)
        # Aditional fluid scalars
        # due to additional degrees of freedom.
        if len(self.fluidvars) > 2:
            # The 𝒫 fluid scalar. Also, if the ς fluid variable exists
            # but is solely used to store 𝒫, mappings for it will not
            # exist yet. Add these as well.
            self.fluid_names[asciify('𝒫')] = (2, 'trace')
            self.fluid_names[unicode('𝒫')] = (2, 'trace')
            self.fluid_names[2, 'trace'  ] = (2, 'trace')
            self.fluid_names[asciify('ς')] = 2
            self.fluid_names[unicode('ς')] = 2
            self.fluid_names[2           ] = 2
        # Also include particle variable names in the fluid_names dict
        self.fluid_names['pos'] = 0
        self.fluid_names['mom'] = 1
        # Assign the fluid variables and scalars as conveniently
        # named attributes on the Component instance.
        # Use the same naming scheme as above.
        try:
            if len(self.fluidvars) > 0:
                self.ϱ   = self.fluidvars[0][0]
            if len(self.fluidvars) > 1:
                self.J   = self.fluidvars[1]
                self.Jx  = self.fluidvars[1][0]
                self.Jy  = self.fluidvars[1][1]
                self.Jz  = self.fluidvars[1][2]
            if len(self.fluidvars) > 2:
                self.ς   = self.fluidvars[2]
                self.𝒫   = self.fluidvars[2]['trace']
                self.ςxx = self.fluidvars[2][0, 0]
                self.ςxy = self.fluidvars[2][0, 1]
                self.ςxz = self.fluidvars[2][0, 2]
                self.ςyx = self.fluidvars[2][1, 0]
                self.ςyy = self.fluidvars[2][1, 1]
                self.ςyz = self.fluidvars[2][1, 2]
                self.ςzx = self.fluidvars[2][2, 0]
                self.ςzy = self.fluidvars[2][2, 1]
                self.ςzz = self.fluidvars[2][2, 2]
        except (IndexError, KeyError):
            pass

    # Function which returns the constant background density
    # ϱ_bar = a**(3*(1 + w_eff(a)))*ρ_bar(a).
    @property
    def ϱ_bar(self):
        if self._ϱ_bar == -1:
            if enable_class_background:
                cosmoresults = compute_cosmo(
                    class_call_reason=f'in order to determine ̅ϱ of {self.name} ',
                )
                self._ϱ_bar = cosmoresults.ρ_bar(1, self.class_species)
            elif self.representation == 'particles':
                if self.mass == -1:
                    abort(
                        f'Cannot determine ϱ_bar for particle component "{self.name}" because '
                        f'its mass is not (yet?) set and enable_class_background is False'
                    )
                # This does not hold for decaying species
                self._ϱ_bar = self.N*self.mass/boxsize**3
            elif self.representation == 'fluid':
                if self.mass == -1:
                    self._ϱ_bar, σϱ, ϱ_min = measure(self, 'ϱ')
                    if self._ϱ_bar == 0:
                        masterwarn(
                            f'Failed to measure ̅ϱ of {self.name}. '
                            f'Try specifying the (fluid element) mass.'
                        )
                else:
                    # This does not hold for decaying species
                    self._ϱ_bar = (self.gridsize/boxsize)**3*self.mass
        return self._ϱ_bar

    # Method which returns the decay rate of this component
    @cython.header(
        # Arguments
        a='double',
        # Locals
        class_species=str,
        Γ_class_species='double',
        Γρ_bar='double',
        ρ_bar='double',
        ρ_bar_class_species='double',
        returns='double',
    )
    def Γ(self, a=-1):
        """For components consisting of a combination of
        (CLASS) species, their Γ is defined as
        Γ(a) = (Γ_1*ρ_1_bar(a) + Γ_2*ρ_2_bar(a))/(ρ_1_bar(a) + ρ_2_bar(a)),
        and so Γ is in general time dependent.
        """
        if a == -1:
            a = universals.a
        if self.w_eff_type == 'constant':
            # A constant w_eff implies that the particle/fluid element
            # mass generally given by self.mass*a**(-3*self.w_eff(a=a))
            # is either constant (w = 0) or a power law in a. This is
            # not the behaviour we expect for decaying species.
            return 0
        if not enable_class_background:
            # We cannot compute Γ without the CLASS background
            return 0
        # Compute CLASS background
        cosmoresults = compute_cosmo(class_call_reason=f'in order to determine Γ of {self.name} ')
        # Sum up ρ_bar and Γ*ρ_bar
        ρ_bar = Γρ_bar = 0
        for class_species in self.class_species.split('+'):
            ρ_bar_class_species = cosmoresults.ρ_bar(a, class_species)
            if class_species == 'dcdm':
                Γ_class_species = cosmoresults.Γ_dcdm
            elif class_species == 'dr':
                # Note that dr does not decay by itself but is the
                # decay product of dcdm. We should then use
                # Γ_dr = - ρ_bar_dcdm/ρ_bar_dr*Γ_dcdm
                Γ_class_species = (
                    -cosmoresults.ρ_bar(a, 'dcdm')/ρ_bar_class_species*cosmoresults.Γ_dcdm
                )
            else:
                # CLASS species not implemented above are assumed stable
                Γ_class_species = 0
            ρ_bar += ρ_bar_class_species
            Γρ_bar += Γ_class_species*ρ_bar_class_species
        return Γρ_bar/ρ_bar

    # This method populate the Component pos/mom arrays (for a
    # particles representation) or the fluid scalar grids (for a
    # fluid representation) with data. It is deliberately designed so
    # that you have to make a call for each attribute (posx, posy, ...
    # for particle components, ϱ, Jx, Jy, ... for fluid components).
    # You should construct the data array within the call itself,
    # as this will minimize memory usage. This data array is 1D for
    # particle data and 3D for fluid data.
    @cython.pheader(
        # Arguments
        data=object,  # 1D/3D (particles/fluid) memoryview
        var=object,   # int-like or str
        multi_index=object,  # tuple, int-like or str
        buffer='bint',
        # Locals
        fluid_indices=object,  # tuple or int-like
        fluidscalar='FluidScalar',
        index='Py_ssize_t',
        mv1D='double[::1]',
        mv3D='double[:, :, :]',
    )
    def populate(self, data, var, multi_index=0, buffer=False):
        """For fluids, the data should not include pseudo
        or ghost points.
        If buffer is True, the Δ buffers will be populated
        instead of the data arrays.
        """
        if self.representation == 'particles':
            mv1D = data
            self.N_local = mv1D.shape[0]
            # Enlarge data attributes if necessary
            if self.N_allocated < self.N_local:
                self.resize(self.N_local)
            # Update the data corresponding to the passed string
            if var == 'posx':
                if buffer:
                    self.Δposx_mv[:self.N_local] = mv1D[:]
                else:
                    self.posx_mv [:self.N_local] = mv1D[:]
            elif var == 'posy':
                if buffer:
                    self.Δposy_mv[:self.N_local] = mv1D[:]
                else:
                    self.posy_mv [:self.N_local] = mv1D[:]
            elif var == 'posz':
                if buffer:
                    self.Δposz_mv[:self.N_local] = mv1D[:]
                else:
                    self.posz_mv [:self.N_local] = mv1D[:]
            elif var == 'momx':
                if buffer:
                    self.Δmomx_mv[:self.N_local] = mv1D[:]
                else:
                    self.momx_mv [:self.N_local] = mv1D[:]
            elif var == 'momy':
                if buffer:
                    self.Δmomy_mv[:self.N_local] = mv1D[:]
                else:
                    self.momy_mv [:self.N_local] = mv1D[:]
            elif var == 'momz':
                if buffer:
                    self.Δmomz_mv[:self.N_local] = mv1D[:]
                else:
                    self.momz_mv [:self.N_local] = mv1D[:]
            elif master:
                abort('Wrong component attribute name "{}"!'.format(var))
        elif self.representation == 'fluid':
            mv3D = data
            # The fluid scalar will be given as
            # self.fluidvars[index][multi_index],
            # where index is an int and multi_index is a tuple of ints.
            # These may be given directly as var and multi_index.
            # Alternatively, var may be a str, in which case it can be
            # the name of a fluid variable or a fluid scalar.
            # For each possibility, find index and multi_index.
            if isinstance(var, int):
                if not (0 <= var < len(self.fluidvars)):
                    abort('The "{}" component does not have a fluid variable with index {}'
                          .format(self.name, var))
                # The fluid scalar is given as
                # self.fluidvars[index][multi_index].
                index = var
            if isinstance(var, str):
                if var not in self.fluid_names:
                    abort('The "{}" component does not contain a fluid variable with the name "{}"'
                          .format(self.name, var))
                # Lookup the fluid indices corresponding to var.
                # This can either be a tuple of the form
                # (index, multi_index) (for a passed fluid scalar name)
                # or just an index (for a passed fluid name).
                fluid_indices = self.fluid_names[var]
                if isinstance(fluid_indices, int):
                    index = fluid_indices
                else:  # fluid_indices is a tuple
                    if multi_index != 0:
                        masterwarn('Overwriting passed multi_index ({}) with {}, '
                                   'deduced from the passed var = {}.'
                                   .format(multi_index, fluid_indices[1], var)
                                   )
                    index, multi_index = fluid_indices
            # Type check on the multi_index
            if not isinstance(multi_index, (int, tuple, str)):
                abort(
                    f'A multi_index of type "{type(multi_index)}" was supplied. '
                    f'This should be either an int, a tuple or a str.'
                )
            # Reallocate fluid grids if necessary
            self.resize(asarray(mv3D).shape)
            # Populate the scalar grid given by index and multi_index
            # with the passed data. This data should not
            # include pseudo or ghost points.
            fluidscalar = self.fluidvars[index][multi_index]
            if buffer:
                fluidscalar.Δ_noghosts[
                    :mv3D.shape[0], :mv3D.shape[1], :mv3D.shape[2]] = mv3D[...]
            else:
                fluidscalar.grid_noghosts[
                    :mv3D.shape[0], :mv3D.shape[1], :mv3D.shape[2]] = mv3D[...]
            # Populate pseudo and ghost points
            if buffer:
                communicate_domain(fluidscalar.Δ_mv   , mode='populate')
            else:
                communicate_domain(fluidscalar.grid_mv, mode='populate')

    # This method will grow/shrink the data attributes.
    # Note that it will update N_allocated but not N_local.
    @cython.pheader(
        # Arguments
        size_or_shape_nopseudo_noghosts=object,  # Py_ssize_t or tuple
        # Locals
        fluidscalar='FluidScalar',
        i='Py_ssize_t',
        s='Py_ssize_t',
        shape_nopseudo_noghosts=tuple,
        size='Py_ssize_t',
        s_old='Py_ssize_t',
    )
    def resize(self, size_or_shape_nopseudo_noghosts):
        if self.representation == 'particles':
            size = size_or_shape_nopseudo_noghosts
            if size != self.N_allocated:
                self.N_allocated = size
                # Reallocate particle data
                self.posx = realloc(self.posx, self.N_allocated*sizeof('double'))
                self.posy = realloc(self.posy, self.N_allocated*sizeof('double'))
                self.posz = realloc(self.posz, self.N_allocated*sizeof('double'))
                self.momx = realloc(self.momx, self.N_allocated*sizeof('double'))
                self.momy = realloc(self.momy, self.N_allocated*sizeof('double'))
                self.momz = realloc(self.momz, self.N_allocated*sizeof('double'))
                # Reassign particle data memory views
                self.posx_mv = cast(self.posx, 'double[:self.N_allocated]')
                self.posy_mv = cast(self.posy, 'double[:self.N_allocated]')
                self.posz_mv = cast(self.posz, 'double[:self.N_allocated]')
                self.momx_mv = cast(self.momx, 'double[:self.N_allocated]')
                self.momy_mv = cast(self.momy, 'double[:self.N_allocated]')
                self.momz_mv = cast(self.momz, 'double[:self.N_allocated]')
                # Repack particle data into pointer arrays of pointers
                # and lists of memoryviews.
                self.pos[0], self.pos[1], self.pos[2] = self.posx, self.posy, self.posz
                self.mom[0], self.mom[1], self.mom[2] = self.momx, self.momy, self.momz
                self.pos_mv = [self.posx_mv, self.posy_mv, self.posz_mv]
                self.mom_mv = [self.momx_mv, self.momy_mv, self.momz_mv]
                # Reallocate particle buffers
                # (commented as these are not currently used).
                #self.Δposx = realloc(self.Δposx, self.N_allocated*sizeof('double'))
                #self.Δposy = realloc(self.Δposy, self.N_allocated*sizeof('double'))
                #self.Δposz = realloc(self.Δposz, self.N_allocated*sizeof('double'))
                self.Δmomx = realloc(self.Δmomx, self.N_allocated*sizeof('double'))
                self.Δmomy = realloc(self.Δmomy, self.N_allocated*sizeof('double'))
                self.Δmomz = realloc(self.Δmomz, self.N_allocated*sizeof('double'))
                # Reassign particle buffer memory views
                # (commented as these are not currently used).
                #self.Δposx_mv = cast(self.Δposx, 'double[:self.N_allocated]')
                #self.Δposy_mv = cast(self.Δposy, 'double[:self.N_allocated]')
                #self.Δposz_mv = cast(self.Δposz, 'double[:self.N_allocated]')
                self.Δmomx_mv = cast(self.Δmomx, 'double[:self.N_allocated]')
                self.Δmomy_mv = cast(self.Δmomy, 'double[:self.N_allocated]')
                self.Δmomz_mv = cast(self.Δmomz, 'double[:self.N_allocated]')
                # Repack particle buffers into pointer arrays of
                # pointers and lists of memoryviews.
                self.Δpos[0], self.Δpos[1], self.Δpos[2] = self.Δposx, self.Δposy, self.Δposz
                self.Δmom[0], self.Δmom[1], self.Δmom[2] = self.Δmomx, self.Δmomy, self.Δmomz
                self.Δpos_mv = [self.Δposx_mv, self.Δposy_mv, self.Δposz_mv]
                self.Δmom_mv = [self.Δmomx_mv, self.Δmomy_mv, self.Δmomz_mv]
                # Nullify the newly allocated Δ buffer
                self.nullify_Δ()
                # Reallocate rung indices and jumps, if rungs are in use
                if self.use_rungs:
                    self.rung_indices = realloc(
                        self.rung_indices,
                        self.N_allocated*sizeof('signed char'),
                    )
                    self.rung_jumps = realloc(
                        self.rung_jumps,
                        self.N_allocated*sizeof('signed char'),
                    )
                    # New particles default to rung 0
                    for i in range(self.rung_indices_mv.shape[0], self.N_allocated):
                        self.rung_indices[i] = 0
                    self.rung_indices_mv = cast(self.rung_indices, 'signed char[:self.N_allocated]')
                    for i in range(self.rung_jumps_mv.shape[0], self.N_allocated):
                        self.rung_jumps[i] = 0
                    self.rung_jumps_mv = cast(self.rung_jumps, 'signed char[:self.N_allocated]')
        elif self.representation == 'fluid':
            shape_nopseudo_noghosts = size_or_shape_nopseudo_noghosts
            # The allocated shape of the fluid grids are 5 points
            # (one layer of pseudo points and two layers of ghost points
            # before and after the local grid) longer than the local
            # shape, in each direction.
            if not any([2 + s + 1 + 2 != s_old for s, s_old in zip(shape_nopseudo_noghosts,
                                                                   self.shape)]):
                return
            if any([s < 1 for s in shape_nopseudo_noghosts]):
                abort('Attempted to resize fluid grids of the {} component '
                      'to a shape of {}'.format(self.name, shape_nopseudo_noghosts))
            # Recalculate and reassign meta data
            self.shape          = tuple([2 + s + 1 + 2 for s in shape_nopseudo_noghosts])
            self.shape_noghosts = tuple([    s + 1     for s in shape_nopseudo_noghosts])
            self.size           = np.prod(self.shape)
            self.size_noghosts  = np.prod(self.shape_noghosts)
            # Reallocate fluid data
            for fluidscalar in self.iterate_fluidscalars():
                fluidscalar.resize(shape_nopseudo_noghosts)

    # Method for 3D realisation of linear transfer functions.
    # As all arguments are optional,
    # this has to be a pure Python method.
    def realize(
        self,
        variables=None,
        transfer_spline=None,
        cosmoresults=None,
        specific_multi_index=None,
        a=-1,
        a_next=-1,
        gauge='N-body',
        options=None,
        use_gridˣ=False,
    ):
        """This method will realise a given fluid/particle variable from
        a given transfer function. Any existing data for the variable
        in question will be lost.
        The variables argument specifies which variable(s) of the
        component to realise. Valid formats of this argument can be seen
        in varnames2indices. If no variables argument is passed,
        transfer functions for each variable will be computed via CLASS
        and all of them will be realized.
        If a specific_multi_index is passed, only the single fluidscalar
        of the variable(s) with the corresponding multi_index
        will be realized. If no specific_multi_index is passed,
        all fluidscalars of the variable(s) will be realized.
        The transfer_spline is a Spline object of the transfer function
        of the variable which should be realised.
        The cosmoresults argument is a linear.CosmoResults object
        containing all results from the CLASS run which produced the
        transfer function, from which further information
        can be obtained.
        Specify the scale factor a if you want to realize the variables
        at a time different from the present time.
        If neither the transfer_spline nor the cosmoresults argument is
        given, these will be produced by calling CLASS.
        You can supply multiple variables in one go,
        but then you have to leave the transfer_spline and cosmoresults
        arguments unspecified (as you can only pass in a
        single transfer_spline).
        The gauge and options arguments are passed on to
        linear.compute_transfer and linear.realize, respectively.
        See these functions for further detail.
        The use_gridˣ argument is passed on to linear.relize and
        determines whether the unstarred or starred grids should be used
        when doing the realization.
        """
        if a == -1:
            a = universals.a
        if options is None:
            options = {}
        options = {key.lower().replace(' ', '').replace('-', ''):
            (val.lower().replace(' ', '').replace('-', '') if isinstance(val, str) else val)
            for key, val in options.items()
        }
        # Define the gridsize used by the realization (gridsize for
        # fluid components and ∛N for particle components) and resize
        # the data attributes if needed.
        # Also do some particles-only checks.
        if self.representation == 'particles':
            if self.N%nprocs != 0:
                abort(f'Cannot perform realization of particle component "{self.name}" '
                      f'with N = {self.N}, as N is not evenly divisible by {nprocs} processes.'
                      )
            if not isint(ℝ[cbrt(self.N)]):
                abort(f'Cannot perform realization of particle component "{self.name}" '
                      f'with N = {self.N}, as N is not a cubic number.'
                      )
            gridsize = int(round(ℝ[cbrt(self.N)]))
            self.N_local = self.N//nprocs
            self.resize(self.N_local)
        elif self.representation == 'fluid':
            gridsize = self.gridsize
            shape = tuple([gridsize//domain_subdivisions[dim] for dim in range(3)])
            self.resize(shape)
        # Check that the gridsize fulfills the requirements for FFT
        # and therefore for realizations.
        if gridsize%nprocs != 0:
            abort(f'Cannot perform realization of component "{self.name}" '
                  f'with gridsize = {gridsize}, as gridsize is not '
                  f'evenly divisible by {nprocs} processes.'
                  )
        for dim in range(3):
            if gridsize%domain_subdivisions[dim] != 0:
                abort(f'Cannot perform realization of component "{self.name}" '
                      f'with gridsize = {gridsize}, as the global grid of shape '
                      f'({gridsize}, {gridsize}, {gridsize}) cannot be divided '
                      f'according to the domain decomposition ({domain_subdivisions[0]}, '
                      f'{domain_subdivisions[1]}, {domain_subdivisions[2]}).'
                      )
        # Argument processing
        if transfer_spline is None and cosmoresults is not None:
            abort('The realize method was called with cosmoresults but no transfer_spline')
        if variables is None:
            if transfer_spline is not None:
                masterwarn('The realize method was called without specifying a variable, '
                           'though a transfer_spline is passed. '
                           'This transfer_spline will be ignored.')
            if cosmoresults is not None:
                masterwarn('The realize method was called without specifying a variable, '
                           'though a cosmoresults is passed. This cosmoresults will be ignored.')
            # Realize all variables
            variables = arange(self.boltzmann_order + 1)
        else:
            # Realize one or more variables
            variables = any2list(self.varnames2indices(variables))
            N_vars = len(variables)
            if N_vars > 1:
                # Realize multiple variables
                if transfer_spline is not None:
                    abort(f'The realize method was called with {N_vars} variables '
                          'while a transfer_spline was supplied as well')
                if cosmoresults is not None:
                    abort(f'The realize method was called with {N_vars} variables '
                          'while cosmoresults was supplied as well')
        # Prepare arguments to compute_transfer,
        # if no transfer_spline is passed.
        if transfer_spline is None:
            k_min, k_max, k_gridsize = get_default_k_parameters(gridsize)
        # Realize each of the variables in turn
        options_passed = options.copy()
        for variable in variables:
            options = options_passed.copy()
            # The special "realization" of 𝒫 when using
            # the P=wρ approximation.
            if (   self.representation == 'fluid'
                and variable == 2
                and specific_multi_index == 'trace'
                and transfer_spline is None
                and self.approximations['P=wρ']
                ):
                self.realize_𝒫(a, use_gridˣ)
            else:
                # Normal realization
                if (self.representation == 'particles'
                    and 'velocitiesfromdisplacements' not in options
                ):
                    # The 'velocities from displacements' option
                    options['velocitiesfromdisplacements'] = self.realization_options['mom'][
                        'velocitiesfromdisplacements']
                    # For particles, the Boltzmann order is always 1,
                    # corresponding to positions and momenta. However,
                    # when velocities are set to be realized from
                    # displacements, the momenta (proportional to the
                    # velocity field uⁱ) are constructed from the
                    # displacement field ψⁱ (using the linear growth
                    # rate f) during the Zel'dovich approximation. Thus,
                    # from a single realization of ψⁱ, both the
                    # positions and the momenta are constructed. We
                    # should then pass only the positions as the
                    # variable to be realized (the realize function will
                    # realize both positions and momenta when velocities
                    # are to be realized from displacements).
                    if options['velocitiesfromdisplacements'] and variable == 1:
                        break
                # The back-scaling option
                if 'backscaling' not in options:
                    if variable == 0:
                        options['backscaling'] = self.realization_options[
                            {'particles': 'pos', 'fluid': 'ϱ'}[self.representation]
                        ]['backscaling']
                    elif variable == 1:
                        options['backscaling'] = self.realization_options[
                            {'particles': 'mom', 'fluid': 'J'}[self.representation]
                        ]['backscaling']
                    elif variable == 2 and specific_multi_index == 'trace':
                        options = self.realization_options['𝒫']['backscaling']
                    elif variable == 2:
                        options = self.realization_options['ς']['backscaling']
                # Get transfer function if not passed
                if transfer_spline is None:
                    # When realizing using the primordial structure
                    # (as opposed to realizing non-linearly),
                    # the realization looks like
                    # ℱₓ⁻¹[T(k) ζ(k) K(k⃗) ℛ(k⃗)],
                    # with ℛ(k⃗) the primordial noise, T(k) = T(a, k) the
                    # transfer function at the specified a, ζ(k) the
                    # primordial curvature perturbations and K(k⃗)
                    # containing any additional tensor structure.
                    # The only time dependent part of the realiztion is
                    # then the transfer function T(a, k). In the case of
                    # linear realization, i.e. a realization of a
                    # field of the same variable number as the Boltzmann
                    # order of the component, we do not actually care
                    # about realizing the exact field, but only about
                    # obtaining precise influences from this field on
                    # other, non-linear fields. For each Boltzmann
                    # order, we list below the corresponding linear
                    # fluid variable together with its most important
                    # evolution equation through which it affects the
                    # rest of the system.
                    # Boltzmann order -1:
                    #     ϱ,     ∇²φ = 4πGa²ρ = 4πGa**(-3*w_eff - 1)ϱ
                    # Boltzmann order 0:
                    #     Jᵐ,    ∂ₜϱ = -a**(3*w_eff - 2)∂ᵢJⁱ  + ⋯
                    # Boltzmann order 1:
                    #     𝒫,    ∂ₜJᵐ = -a**(-3*w_eff)∂ᵐ𝒫      + ⋯
                    #     ςᵐₙ,  ∂ₜJᵐ = -a**(-3*w_eff)∂ⁿςᵐₙ    + ⋯
                    # To take Boltzmann order -1 as an example, this
                    # means we should realize a weighted average of
                    # ϱ(t, k⃗), with a weight given by
                    # a(t)**(-3*w_eff(t) - 1), i.e.
                    # T(a, k) → 1/(ᔑa(t)**(-3*w_eff(t) - 1) dt)
                    #             *ᔑa(t)**(-3*w_eff(t) - 1)T(a, k) dt,
                    # with the integrals ranging over the time step.
                    # Below these weights are represented as str's.
                    # The actual averaging is carried out by the
                    # TransferFunction.as_function_of_k() method.
                    weight = None
                    if (
                            options.get('structure') != 'nonlinear'
                        and self.representation == 'fluid'
                        and self.boltzmann_closure == 'class'
                        and self.boltzmann_order + 1 == variable
                        and a_next != -1
                    ):
                        if variable == 0:
                            weight = 'a**(-3*w_eff-1)'
                        elif variable == 1:
                            weight = 'a**(3*w_eff-2)'
                        elif variable == 2:
                            weight = 'a**(-3*w_eff)'
                    transfer_spline, cosmoresults = compute_transfer(
                        self,
                        variable,
                        k_min, k_max, k_gridsize,
                        specific_multi_index,
                        a,
                        a_next,
                        gauge,
                        weight=weight,
                    )
                # Do the realization
                realize(
                    self,
                    variable,
                    transfer_spline,
                    cosmoresults,
                    specific_multi_index,
                    a,
                    options,
                    use_gridˣ,
                )
                # Reset transfer_spline to None so that a transfer
                # function will be computed for the next variable.
                transfer_spline = None

    # Method for realizing a linear fluid scalar
    def realize_if_linear(
        self,
        variable,
        transfer_spline=None,
        cosmoresults=None,
        specific_multi_index=None,
        a=-1,
        a_next=-1,
        gauge='N-body',
        options=None,
        use_gridˣ=False,
    ):
        """If the fluid scalar is not linear or does not exist at all,
        no realization will be performed and no exception will
        be raised.
        """
        if self.representation == 'particles':
            return
        # Check that the fluid variable exist
        try:
            variable = self.varnames2indices(variable, single=True)
        except (IndexError, KeyError):
            return
        # For all variables other than ϱ (variable == 0),
        # a specific_multi_index has to have been passed.
        if specific_multi_index is None:
            if variable == 0:
                specific_multi_index = 0
            else:
                abort(
                    f'The realize_if_linear function was called with variable = {variable} ≠ 0 '
                    f'but without any specific_multi_index'
                )
        # Check that the fluid scalar exist
        if specific_multi_index not in self.fluidvars[variable]:
            return
        # Get the non-linear realization options
        if options is None:
            if variable == 0:
                options = self.realization_options['ϱ']
            elif variable == 1:
                options = self.realization_options['J']
            elif variable == 2 and specific_multi_index == 'trace':
                options = self.realization_options['𝒫']
            elif variable == 2:
                options = self.realization_options['ς']
            else:
                abort(
                    f'Do not know how to extract realization options '
                    f'for fluid variable {variable}[{specific_multi_index}]'
                )
        # Do the realization if the passed variable really is linear
        if self.is_linear(variable, specific_multi_index):
            self.realize(
                variable,
                transfer_spline,
                cosmoresults,
                specific_multi_index,
                a,
                a_next,
                gauge,
                options,
                use_gridˣ,
            )
    # Method for checking whether a given fluid variable
    # or fluid scalar is linear or non-linear.
    def is_linear(self, variable, specific_multi_index=None):
        """When no specific_multi_index is passed, it as assumed that it
        does not matter which fluid scalar of the variable we check
        for linearity (this is not necessarily the case for ς which may
        store the additional "trace" (𝒫) fluid scalar).
        If a variable is passed that does not exist on the component at
        all, this method will return True. Crucially then, the caller
        must never rely on the fact that the variable is linear
        (only that it is not non-linear), unless it is certain that the
        variable does indeed exist.
        """
        if self.representation == 'particles':
            abort(
                f'The is_linear() method was called on the {self.name} particle component, '
                f'for which the concept of linearity does not make sense'
            )
        # Lookup the variable. Return True if it does not exist.
        try:
            variable = self.varnames2indices(variable, single=True)
        except (IndexError, KeyError):
            return True
        # If no specific_multi_index is passed, construct one
        if specific_multi_index is None:
            if variable == 0:
                specific_multi_index = 0
            else:
                specific_multi_index = (0,)*variable
        # Check the linearity
        return self.fluidvars[variable][specific_multi_index].is_linear

    # Method for realizing 𝒫 when the P=wρ approximation is enabled
    @cython.header(
        # Arguments
        a='double',
        use_gridˣ='bint',
        # Locals
        i='Py_ssize_t',
        ϱ_ptr='double*',
        𝒫_ptr='double*',
    )
    def realize_𝒫(self, a=-1, use_gridˣ=False):
        """This method applies 𝒫 = c²wϱ if the P=wρ approximation
        is enabled. If not, an exception will be thrown. This method
        is called from the more general realize method. It is not the
        intend that this method should be called from anywhere else.
        """
        if a == -1:
            a = universals.a
        if self.approximations['P=wρ']:
            # Set 𝒫 equal to the current ϱ times the current c²w
            if use_gridˣ:
                ϱ_ptr = self.ϱ.gridˣ
                𝒫_ptr = self.𝒫.gridˣ
            else:
                ϱ_ptr = self.ϱ.grid
                𝒫_ptr = self.𝒫.grid
            for i in range(self.size):
                𝒫_ptr[i] = ϱ_ptr[i]*ℝ[light_speed**2*self.w(a=a)]
        else:
            abort(
                f'The realize_𝒫 method was called on the {self.name} component wich have P ≠ wρ. '
                f'You should call the more general realize method instead.'
            )

    # Method for integrating particle positions/fluid values
    # forward in time.
    # For fluid components, source terms are not included.
    @cython.header(
        # Arguments
        ᔑdt=dict,
        a_next='double',
        # Locals
        dim='int',
        i='Py_ssize_t',
        mom_dim='double*',
        pos_dim='double*',
        rk_order='int',
        scheme=str,
    )
    def drift(self, ᔑdt, a_next=-1):
        if self.representation == 'particles':
            # Update positions
            for dim in range(3):
                pos_dim, mom_dim = self.pos[dim], self.mom[dim]
                for i in range(self.N_local):
                    # The factor a**(3*w_eff) is included below to
                    # account for decaying particles, for which the mass
                    # is given by a**(-3*w_eff)*self.mass.
                    # We should not include this time-varying factor
                    # inside the integral, as the reciprocal factor
                    # hides inside the momentum (as it is proportional
                    # to the mass). Furthermore we should not evaluate
                    # this factor at different values dependent on which
                    # short-range rung is being drifted, as the factor
                    # inside the momentum is only applied once
                    # for every base time step (i.e. it is considered
                    # a long-range "force" / source term).
                    pos_dim[i] += mom_dim[i]*ℝ[
                        ᔑdt['a**(-2)']*universals.a**(3*self.w_eff(a=universals.a))/self.mass
                    ]
                    # Toroidal boundaries
                    pos_dim[i] = mod(pos_dim[i], boxsize)
            # Some particles may have drifted out of the local domain.
            # Exchange particles to the correct processes.
            exchange(self)
        elif self.representation == 'fluid':
            # Evolve the fluid due to flux terms using the scheme
            # specified in the user parameters.
            scheme = is_selected(self, fluid_scheme_select)
            if scheme == 'maccormack':
                # For the MacCormack scheme to do anything,
                # the J variable must exist.
                if not (
                        self.boltzmann_order == -1
                    or (self.boltzmann_order == 0 and self.boltzmann_closure == 'truncate')
                ):
                    masterprint(
                        f'Evolving fluid variables (flux terms, using the MacCormack scheme) '
                        f'of {self.name} ...'
                    )
                    maccormack(self, ᔑdt, a_next)
                    masterprint('done')
            elif scheme == 'kurganovtadmor':
                # For the Kurganov-Tadmor scheme to do anything,
                # the J variable must exist.
                if not (
                        self.boltzmann_order == -1
                    or (self.boltzmann_order == 0 and self.boltzmann_closure == 'truncate')
                ):
                    rk_order = is_selected(
                        self, fluid_options['kurganovtadmor']['rungekuttaorder'])
                    masterprint(
                        f'Evolving fluid variables (flux terms, using the Kurganov-Tadmor scheme) '
                        f'of {self.name} ...'
                    )
                    kurganov_tadmor(self, ᔑdt, rk_order=rk_order)
                    masterprint('done')
            else:
                abort(
                    f'It was specified that the {self.name} component should be evolved using '
                    f'the "{scheme}" scheme, which is not implemented.'
                )

    # Method for assigning a rung index (0 to N_rungs - 1)
    # to every local particle, if this component uses rungs.
    @cython.header(
        # Arguments
        Δt='double',
        fac_softening='double',
        # Locals
        a='double',
        i='Py_ssize_t',
        mom2='double',
        momx='double*',
        momy='double*',
        momz='double*',
        rung_index='signed char',
        rung_indices='signed char*',
        rungs_N='Py_ssize_t*',
        v2='double',
        returns='void',
    )
    def assign_rungs(self, Δt, fac_softening):
        # When not using rungs, all particles occupy rung 0.
        # The rung indices themselves are not needed.
        if not self.use_rungs:
            self.rungs_N[0] = self.N_local
            return
        a = universals.a
        # Extract variables
        momx         = self.momx
        momy         = self.momy
        momz         = self.momz
        rung_indices = self.rung_indices
        rungs_N      = self.rungs_N
        # Reset the particle count for each rung
        for rung_index in range(N_rungs):
            rungs_N[rung_index] = 0
        # Assign a rung to each particle
        for i in range(self.N_local):
            # Get the (squared) velocity in comoving units
            mom2 = momx[i]**2 + momy[i]**2 + momz[i]**2
            v2 = mom2*ℝ[1/(a**(2 - 3*self.w_eff(a=a))*self.mass)**2]
            # Determine the rung. This is an integer between 0
            # (corresponding to the base time step size Δt)
            # and N_rungs - 1. Each rung uses half the time step size
            # of the previous rung.
            if v2 <= ℝ[(fac_softening*self.softening_length/Δt)**2]:
                rung_index = 0
            else:
                rung_index = cast(
                    0.5*log2(v2*ℝ[(Δt/(fac_softening*self.softening_length))**2]),
                    'Py_ssize_t',
                ) + 1
                if rung_index > ℤ[N_rungs - 1]:
                    rung_index = ℤ[N_rungs - 1]
            # Register this rung with the particle
            rung_indices[i] = rung_index
            rungs_N[rung_index] += 1
        # Flag the lowest and highest populated rungs
        self.set_lowest_highest_populated_rung()

    # Method for flagging particle inter-rung jumps to come
    @cython.header(
        # Arguments
        Δt='double',
        rung_integrals='double[::1]',
        fac_softening='double',
        Δt_downjump_fac='double',
        # Locals
        a='double',
        any_rung_jumps='bint',
        i='Py_ssize_t',
        lowest_active_rung='signed char',
        mom2='double',
        momx='double*',
        momy='double*',
        momz='double*',
        rung_index='signed char',
        rung_indices='signed char*',
        rung_jump='signed char',
        rung_jumps='signed char*',
        v2='double',
        v2_max='double',
        v2_min='double',
        returns='bint',
    )
    def flag_rung_jumps(self, Δt, rung_integrals, fac_softening, Δt_downjump_fac):
        # When not using rungs, all particles occupy rung 0,
        # and no rung jumps are possible.
        if not self.use_rungs:
            return False
        a = universals.a
        # Extract variables
        momx               = self.momx
        momy               = self.momy
        momz               = self.momz
        rung_indices       = self.rung_indices
        rung_jumps         = self.rung_jumps
        lowest_active_rung = self.lowest_active_rung
        # Check for inter-rung jumps for each active particle
        any_rung_jumps = False
        for i in range(self.N_local):
            rung_index = rung_indices[i]
            if rung_index < lowest_active_rung:
                continue
            # Get the (squared) velocity in comoving units
            mom2 = momx[i]**2 + momy[i]**2 + momz[i]**2
            v2 = mom2*ℝ[1/(a**(2 - 3*self.w_eff(a=a))*self.mass)**2]
            # Get maximum allowed v² at the current rung
            # of this particle.
            v2_max = (2**cast(2*rung_index, 'Py_ssize_t')
                *ℝ[(fac_softening*self.softening_length/Δt)**2])
            # If the particle velocity is larger than allowed
            # for this rung, jump up to the next rung.
            if v2 > v2_max:
                if rung_index < ℤ[N_rungs - 1]:
                    rung_jumps[i] = +1
                    any_rung_jumps = True
                continue
            # Particle should not jump up.
            # If at rung zero, it cannot jump down either.
            if rung_index == 0:
                continue
            # Jumping down is only allowed every second kick, for any
            # particular rung. When jumping down is disallowed, the
            # down jumping rung integrals are set to -1.
            if rung_integrals[rung_index + N_rungs] == -1:
                # Jumping down disallowed
                continue
            # Get minimum allowed v² at the current rung
            # of this particle.
            v2_min = 0.25*v2_max
            # If the particle velocity is below Δt_downjump_fac
            # times the minimum velocity for this rung,
            # jump down to the rung below.
            if v2 < ℝ[Δt_downjump_fac**2]*v2_min:
                rung_jumps[i] = -1
                any_rung_jumps = True
                continue
        return any_rung_jumps

    # Method for applying flagged particle inter-rung jumps
    @cython.header(
        # Locals
        i='Py_ssize_t',
        rung_index='signed char',
        rung_indices='signed char*',
        rung_jump='signed char',
        rung_jumps='signed char*',
        rungs_N='Py_ssize_t*',
        returns='void',
    )
    def apply_rung_jumps(self):
        rung_indices = self.rung_indices
        rung_jumps   = self.rung_jumps
        rungs_N      = self.rungs_N
        for i in range(self.N_local):
            rung_jump = rung_jumps[i]
            if rung_jump == 0:
                continue
            # Particle i undergoes an inter-rung jump
            rung_index = rung_indices[i]
            rungs_N[rung_index] -= 1
            rung_index += rung_jump
            rungs_N[rung_index] += 1
            rung_indices[i] = rung_index
            rung_jumps[i] = 0
        self.set_lowest_highest_populated_rung()

    # Method for setting the (local) lowest and highest populated rung
    @cython.header(
        # Locals
        highest_populated_rung='signed char',
        lowest_populated_rung='signed char',
        rung_index='signed char',
        returns='void',
    )
    def set_lowest_highest_populated_rung(self):
        lowest_populated_rung = ℤ[N_rungs - 1]
        highest_populated_rung = 0
        for rung_index in range(N_rungs):
            if self.rungs_N[rung_index] > 0:
                lowest_populated_rung = rung_index
                break
        for rung_index in range(N_rungs - 1, -1, -1):
            if self.rungs_N[rung_index] > 0:
                highest_populated_rung = rung_index
                break
        self.lowest_populated_rung = lowest_populated_rung
        self.highest_populated_rung = highest_populated_rung

    # Method for initializing a tiling
    @cython.header(
        # Arguments
        tiling_name=str,
        initial_rung_size=object,  # sequence of length N_rungs or int-like
        # Locals
        coarse_tiling='Tiling',
        extent='double[::1]',
        location='double[::1]',
        refinement_period='Py_ssize_t',
        rung_index='signed char',
        shape=object,  # sequence of length 3 or 2 or int-like
        tiling='Tiling',
        returns='Tiling',
    )
    def init_tiling(self, tiling_name, initial_rung_size=-1):
        # Do nothing if the tiling is already initialized
        # on this component.
        tiling = self.tilings.get(tiling_name)
        if tiling is not None:
            return tiling
        # Different tilings specified below
        refinement_period = 0
        if tiling_name == 'trivial':
            # This tiling spans the box using a single tile,
            # resulting in no actual tiling. It is useful since the
            # rung-ordering of particles is done at the tile level.
            shape = (1, 1, 1)
            tiling_shapes[tiling_name] = asarray(shape, dtype=C2np['Py_ssize_t'])
            # The rungs within the tile start out with half
            # of the mean required memory per rung.
            if initial_rung_size == -1:
                initial_rung_size = [
                    self.rungs_N[rung_index]//2
                    for rung_index in range(N_rungs)
                ]
            # The extent of the entire tiling,
            # i.e. the extent of the box.
            extent = asarray([boxsize]*3, dtype=C2np['double'])
            # The position of the beginning of the tiling,
            # i.e. the left, backward, lower corner of the box.
            location = zeros(3, dtype=C2np['double'])
        elif tiling_name == 'gravity (tiles)':
            # This tiling is used for the P³M method for gravity.
            # The same tiling is applied to all domains. The tile
            # decomposition on a domain will have a general shape
            # of shape[0]×shape[1]×shape[2], with shape[dim] determined
            # by the criterion that a tile must be at least as large as
            # the short-range cutoff length, in all directions. At the
            # same time, we want to maximize the number of tiles.
            if tiling_name not in tiling_shapes:
                shape = asarray(
                    (boxsize/asarray(domain_subdivisions))/shortrange_params['gravity']['cutoff'],
                    dtype=C2np['Py_ssize_t'],
                )
                masterprint(f'Gravitational tile decomposition: {shape[0]}×{shape[1]}×{shape[2]}')
                tiling_shapes[tiling_name] = shape
            else:
                shape = tiling_shapes[tiling_name]
            # We need this tiling to have a size of at least 3
            # in every direction.
            if np.min(shape) < 3:
                message = [
                    f'For the P³M method, the domain tiling needs a subdivision of at least 3 '
                    f'in every direction. Try increasing φ_gridsize.'
                ]
                if 1 != nprocs != int(round(cbrt(nprocs)))**3:
                    message.append(
                        'It may also help to choose a lower and/or cubic number of processes.'
                    )
                abort(' '.join(message))
            # The rungs within each tile start out with half
            # of the mean required memory per rung.
            if initial_rung_size == -1:
                initial_rung_size = [
                    self.rungs_N[rung_index]//(2*np.prod(shape))
                    for rung_index in range(N_rungs)
                ]
            # The extent of the entire tiling,
            # i.e. the extent of the domain.
            extent = asarray((domain_size_x, domain_size_y, domain_size_z),
                dtype=C2np['double'])
            # The position of the beginning of the tiling,
            # i.e. the left, backward, lower corner of this domain.
            location = asarray((domain_start_x, domain_start_y, domain_start_z),
                dtype=C2np['double'])
        elif tiling_name == 'gravity (subtiles)':
            # Grab the constant refinement period,
            # if automatic subtiling refinement is enabled.
            shape = shortrange_params['gravity']['subtiling']
            if 𝔹[shape[0] == 'automatic']:
                refinement_period = shape[1]
            # The entire (sub)tiling currently being initialized lives
            # within one tile of the "gravity (tiles)" tiling.
            # Get this coarser tiling.
            coarse_tiling = self.tilings.get('gravity (tiles)')
            if coarse_tiling is None:
                abort(
                    'Cannot initialize the "gravity (subtiles)" tiling '
                    'without first having the "gravity (tiles)" tiling initialized'
                )
            # Get the shape of the subtiling
            if tiling_name not in tiling_shapes:
                if 𝔹[shape[0] == 'automatic']:
                    # The subtiling shape is to be determined
                    # automatically. It is optimal to have the subtiles
                    # be as cubic as possible. Additionally, we have
                    # found that having (on average) ~8–14
                    # particles/subtile is optimal, when the particles
                    # are not too clustered. Here we pick the most cubic
                    # choice of all possible subtiling shapes which
                    # leads to subtiles of a volume comparable to the
                    # above stated number of particles.
                    particles_per_subtile_min, particles_per_subtile_max = 8, 14
                    tiling_global_shape = asarray(
                        asarray(domain_subdivisions)*asarray(coarse_tiling.shape),
                        dtype=C2np['double'],
                    )
                    shape_candidates = []
                    for particles_per_subtile in (
                        particles_per_subtile_min, particles_per_subtile_max,
                    ):
                        shape = np.prod(tiling_global_shape)/tiling_global_shape
                        shape *= (float(self.N)/
                            (nprocs*np.prod(asarray(coarse_tiling.shape)*shape)
                                *particles_per_subtile)
                        )**ℝ[1/3]
                        shape = asarray(np.round(shape), dtype=C2np['Py_ssize_t'])
                        shape[shape == 0] = 1
                        shape_candidates.append(shape)
                    shape_diff = shape_candidates[0] - shape_candidates[1]
                    shape_base = shape_candidates[1]
                    shape_candidates = {}
                    for         i in range(shape_diff[0] + 1):
                        for     j in range(shape_diff[1] + 1):
                            for k in range(shape_diff[2] + 1):
                                shape = shape_base + np.array((i, j, k))
                                subtiling_global_shape = tiling_global_shape*shape
                                particles_per_subtile = (
                                    float(self.N)/np.prod(subtiling_global_shape)
                                )
                                # Construct tuple key to be used to
                                # store this shape. The lower the value
                                # of each element in the key, the better
                                # we consider this key to be.
                                key = []
                                noncubicness = np.max((
                                    np.max(subtiling_global_shape)**3
                                        /np.prod(subtiling_global_shape),
                                    np.prod(subtiling_global_shape)
                                        /np.min(subtiling_global_shape)**3,
                                ))
                                key.append(noncubicness)
                                ratio = particles_per_subtile/(
                                    (particles_per_subtile_min*particles_per_subtile_max)**0.5
                                )
                                if ratio < 1:
                                    ratio = 1/ratio
                                key.append(ratio)
                                shape_candidates[tuple(key)] = shape
                    # Pick the shape with the smallest key
                    shape = shape_candidates[sorted(shape_candidates)[0]]
                    # Always use a subtile decomposition of at least 2
                    # in each direction, unless this leads to more
                    # subtiles than particles.
                    shape_atleast2 = shape.copy()
                    shape_atleast2[shape_atleast2 == 1] = 2
                    if np.prod(tiling_global_shape*shape_atleast2) < self.N:
                        shape = shape_atleast2
                shape = asarray(shape, dtype=C2np['Py_ssize_t'])
                masterprint(
                    f'Gravitational subtile decomposition: {shape[0]}×{shape[1]}×{shape[2]}'
                )
                tiling_shapes[tiling_name] = shape
            else:
                shape = tiling_shapes[tiling_name]
            # The rungs within each subtile start out with half
            # of the mean required memory per rung.
            if initial_rung_size == -1:
                initial_rung_size = [
                    self.rungs_N[rung_index]//(2*int(np.prod(shape)*np.prod(coarse_tiling.shape)))
                    for rung_index in range(N_rungs)
                ]
            # The extent of the entire (sub)tiling,
            # i.e. the extent of a coarse tile.
            extent = coarse_tiling.tile_extent
            # As this same (sub)tiling will be used for all of the
            # coarse tiles, the location of this (sub)tiling is
            # not static. We thus do not care about the initial value
            # of the tiling location.
            location = None
        else:
            abort(f'Tiling with name "{tiling_name}" not implemented in Component.tile_sort()')
        # Instantiate Tiling instance
        if initial_rung_size == -1:
            initial_rung_size = 0
        tiling = Tiling(tiling_name, self, shape, extent, initial_rung_size, refinement_period)
        self.tilings[tiling_name] = tiling
        # Relocate the tiling if an initial location has been specified
        if location is not None:
            tiling.relocate(location)
        return tiling

    # Method for sorting particles according to short-range tiles
    @cython.header(
        # Arguments
        tiling_name=str,
        coarse_tiling='Tiling',
        coarse_tile_index='Py_ssize_t',
        subtiling_name=str,
        # Locals
        N_subtiles='Py_ssize_t',
        count='Py_ssize_t',
        dim='int',
        highest_populated_rung='signed char',
        i='Py_ssize_t',
        lowest_populated_rung='signed char',
        quantity='int',
        quantityx='double*',
        quantityy='double*',
        quantityz='double*',
        rung='Py_ssize_t*',
        rung_particle_index='Py_ssize_t',
        rungs_N='Py_ssize_t*',
        subtile='Py_ssize_t**',
        subtile_index='Py_ssize_t',
        subtiles='Py_ssize_t***',
        subtiles_contain_particles='signed char*',
        subtiles_rungs_N='Py_ssize_t**',
        subtiling='Tiling',
        tile_extent='double[::1]',
        tile_index='Py_ssize_t',
        tile_index3D='Py_ssize_t[::1]',
        tiles_contain_particles='signed char*',
        tiling='Tiling',
        tiling_location='double[::1]',
        tmpx='double*',
        tmpy='double*',
        tmpz='double*',
        returns='void',
    )
    def tile_sort(self, tiling_name, coarse_tiling=None, coarse_tile_index=-1,
        subtiling_name=''):
        # Get the tiling from the passed tiling_name
        tiling = self.tilings.get(tiling_name)
        if tiling is None:
            # This tiling has not yet been instantiated
            # on this component. Do it now.
            tiling = self.init_tiling(tiling_name)
        # Perform tile sort
        tiling.sort(coarse_tiling, coarse_tile_index)
        # When a subtiling_name is supplied, this signals an in-memory
        # sorting of the pos and mom data arrays of the particles,
        # so that the order in memory matches that of the particle
        # visiting order when iterating over the tiles and subtiles.
        if not subtiling_name:
            return
        # Extract variables
        lowest_populated_rung  = self.lowest_populated_rung
        highest_populated_rung = self.highest_populated_rung
        tiles_contain_particles = tiling.contain_particles
        tiling_location         = tiling.location
        tile_extent             = tiling.tile_extent
        subtiling = self.tilings.get(subtiling_name)
        if subtiling is None:
            subtiling = self.init_tiling(subtiling_name)
        subtiles                   = subtiling.tiles
        N_subtiles                 = subtiling.size
        subtiles_contain_particles = subtiling.contain_particles
        # Iterate over the tiles and subtiles while keeping a counter
        # keeping track of the particle visiting number. We copy the
        # particle positions and momenta to a temporary buffer using the
        # visiting order. After the iteration we then copy this sorted
        # buffer into the original data arrays. We use the Δmom buffers
        # as the temporary buffers, as these should not store any data
        # at the time of calling this method.
        tmpx = self.Δmomx
        tmpy = self.Δmomy
        tmpz = self.Δmomz
        for quantity in range(2):
            if quantity == 0:
                quantityx = self.momx
                quantityy = self.momy
                quantityz = self.momz
            else:  # quantity == 1
                quantityx = self.posx
                quantityy = self.posy
                quantityz = self.posz
            count = 0
            # Loop over all tiles
            for tile_index in range(tiling.size):
                if tiles_contain_particles[tile_index] == 0:
                    continue
                # Sort particles within the tile into subtiles
                tile_index3D = tiling.tile_index3D(tile_index)
                for dim in range(3):
                    tile_location[dim] = tiling_location[dim] + tile_index3D[dim]*tile_extent[dim]
                subtiling.relocate(tile_location)
                subtiling.sort(tiling, tile_index)
                subtiles_rungs_N = subtiling.tiles_rungs_N
                # Loop over all subtiles in the tile
                for subtile_index in range(N_subtiles):
                    if subtiles_contain_particles[subtile_index] == 0:
                        continue
                    subtile = subtiles[subtile_index]
                    rungs_N = subtiles_rungs_N[subtile_index]
                    # Loop over all rungs in the subtile
                    for rung_index in range(lowest_populated_rung, ℤ[highest_populated_rung + 1]):
                        rung_N = rungs_N[rung_index]
                        if rung_N == 0:
                            continue
                        rung = subtile[rung_index]
                        # Loop over all particles in the rung
                        for rung_particle_index in range(rung_N):
                            particle_index = rung[rung_particle_index]
                            # Copy the data to the temporary buffers
                            tmpx[count] = quantityx[particle_index]
                            tmpy[count] = quantityy[particle_index]
                            tmpz[count] = quantityz[particle_index]
                            count += 1
            # Copy the sorted data back into the data arrays
            for i in range(self.N_local):
                quantityx[i] = tmpx[i]
            for i in range(self.N_local):
                quantityy[i] = tmpy[i]
            for i in range(self.N_local):
                quantityz[i] = tmpz[i]
        # Finally we need to re-sort the tiling
        tiling.sort(coarse_tiling, coarse_tile_index)

    # Method for integrating fluid values forward in time
    # due to "internal" source terms, meaning source terms that do not
    # result from interacting with other components.
    @cython.header(
        # Arguments
        ᔑdt=dict,
        a_next='double',
        # Locals
        J_dim='double*',
        J_dim_fluidscalar='FluidScalar',
        a='double',
        dim='int',
        i='Py_ssize_t',
        mom_decay_factor='double',
        mom_dim='double*',
        scheme=str,
    )
    def apply_internal_sources(self, ᔑdt, a_next=-1):
        # Decaying components should have their momentum reduced due
        # to loss of mass. As the mass at time a is given by
        # self.mass*a**(-3*self.w_eff(a)),
        # the mass (and hence momentum) reduction over the time step
        # can be written as the ratio of this expression evaluated
        # at both ends of the time step.
        # Note that this will keep the velocity
        # au = a²ẋ = a²dx/dt = q/m
        # constant with respect to the decay (external forces like
        # gravity will of course change this velocity).
        # For fluid components, the conserved momentum is
        # J = a⁴(ρ + P)u = (ϱ + c⁻²𝒫)*a**(-3*w_eff)*au.
        # For species with w_eff = 0 (and hence w = 0), we indeed have
        # constant au. When w_eff != 0 due to decay, J is still
        # conserved while au is not, which is wrong. Thus we need the
        # same correction factor on fluids.
        # Importantly, this prescription breaks down for stable
        # components which nonetheless have non-zero w_eff. This ought
        # to be sorted out, which would require separating the two
        # effects (changing equation of state and decay) so that they
        # are not both relayed by w_eff. For now we check this explicity
        # and emit a warning if an inconsistent component is used.
        # Currently the only decaying CLASS species implemented is dcdm.
        # We really should include dr as well, which now gains
        # mass/energy rather than loose it. It hower has
        # w_eff = 1/3 != 0 and so we cannot disinguish between changes
        # in mass due to decay of dcdm and due to redshifting.
        a = universals.a
        if a_next == -1:
            mom_decay_factor = 1
        else:
            mom_decay_factor = a**(3*self.w_eff(a=a))/a_next**(3*self.w_eff(a=a_next))
        if mom_decay_factor != 1 and 'dcdm' in self.class_species.split('+'):
            # Check that the species of this component are legal
            legal_class_species = {'dcdm', 'cdm', 'b'}
            if not all([class_species in legal_class_species
                for class_species in self.class_species.split('+')]):
                masterwarn(
                    f'Currently you must not mix species with non-zero w_eff together with '
                    f'decaying species in a single component. This is the case for '
                    f'"{component.name}" with CLASS species "{self.class_species}".'
                )
            # Reduce all momenta, corresponding to the loss of mass
            if self.representation == 'particles':
                for dim in range(3):
                    mom_dim = self.mom[dim]
                    for i in range(self.N_local):
                        mom_dim[i] *= mom_decay_factor
            elif self.representation == 'fluid' and not self.is_linear(1):
                for dim in range(3):
                    J_dim_fluidscalar = self.J[dim]
                    J_dim = J_dim_fluidscalar.grid
                    for i in range(self.size):
                        J_dim[i] *= mom_decay_factor
        # Representation specific internal source terms below
        if self.representation == 'particles':
            # Particle components have no special internal source terms
            pass
        elif self.representation == 'fluid':
            # Below follow internal source terms not taken into account
            # by the MacCormack/Kurganov-Tadmor methods.
            scheme = is_selected(self, fluid_scheme_select)
            if scheme == 'maccormack':
                # For the MacCormack scheme we have three internal
                # source terms: The Hubble term in the continuity
                # equation and the pressure and shear terms in
                # the Euler equation.
                if (
                    (   # The Hubble term
                            self.boltzmann_order > -1
                        and not self.approximations['P=wρ']
                        and enable_Hubble
                    )
                    or
                    (   # The pressure term
                            self.boltzmann_order > 0
                        and not (self.w_type == 'constant' and self.w_constant == 0)
                    )
                    or
                    (
                        # The shear term
                            self.boltzmann_order > 1
                        or (self.boltzmann_order == 1 and self.boltzmann_closure == 'class')
                    )
                ):
                    masterprint(
                        f'Evolving fluid variables (internal source terms) of {self.name} ...'
                    )
                    maccormack_internal_sources(self, ᔑdt, a_next)
                    masterprint('done')
            elif scheme == 'kurganovtadmor':
                # Only the Hubble term in the continuity equation
                # exist as an internal source term when using
                # the Kurganov Tadmor scheme.
                if (
                    # The Hubble term
                        self.boltzmann_order > -1
                    and not self.approximations['P=wρ']
                    and enable_Hubble
                ):
                    masterprint(
                        f'Evolving fluid variables (internal source terms) of {self.name} ...'
                    )
                    kurganov_tadmor_internal_sources(self, ᔑdt)
                    masterprint('done')
            else:
                abort(
                    f'It was specified that the {self.name} component should be evolved using '
                    f'the "{scheme}" scheme, which is not implemented.'
                )

    # Method for computing the equation of state parameter w
    # at a certain time t or value of the scale factor a.
    # This has to be a pure Python function, otherwise it cannot
    # be called as w(a=a).
    def w(self, *, t=-1, a=-1):
        """This method should not be called before w has been
        initialized by the initialize_w method.
        """
        # If no time or scale factor value is passed,
        # use the current time and scale factor value.
        if t == -1 == a:
            t = universals.t
            a = universals.a
        # Compute w dependent on its type
        if self.w_type == 'constant':
            value = self.w_constant
        elif self.w_type == 'tabulated (t)':
            if t == -1:
                t = cosmic_time(a)
            value = self.w_spline.eval(t)
        elif self.w_type == 'tabulated (a)':
            if a == -1:
                a = scale_factor(t)
            value = self.w_spline.eval(a)
        elif self.w_type == 'expression':
            if t == -1:
                t = cosmic_time(a)
            elif a == -1:
                a = scale_factor(t)
            units_dict['t'] = t
            units_dict['a'] = a
            value = eval_unit(self.w_expression, units_dict)
            units_dict.pop('t')
            units_dict.pop('a')
        else:
            abort(f'Did not recognize w type "{self.w_type}"')
        # For components with a non-linear evolution of ϱ,
        # we cannot handle w ≤ -1, as (ϱ + c⁻²𝒫) becomes non-positive.
        # This really should not be a problem, but the current fluid
        # implementation computes J/(ϱ + c⁻²𝒫) while solving the
        # continuity equation. If what is being run is not a simulation
        # but the CLASS utility, this is not a problem as the system
        # is not to be evolved.
        if value <= -1 and special_params.get('special') != 'CLASS':
            if (
                    (   self.boltzmann_order > 0
                     or (self.boltzmann_order == 0 and self.boltzmann_closure == 'class'))
                and (a > universals.a_begin or t > universals.t_begin)
            ):
                if t == -1:
                    t = cosmic_time(a)
                elif a == -1:
                    a = scale_factor(t)
                abort(
                    f'The equation of state parameter w for {self.name} took on the value '
                    f'{value} ≤ -1 at t = {t} {unit_time}, a = {a}. '
                    f'Such phantom w is not currently allowed for components with non-linear ϱ.'
                )
        # For components with a non-linear evolution of J,
        # we cannot handle w < 0, as the sound speed c*sqrt(w) becomes
        # negative. If what is being run is not a simulation
        # but the CLASS utility, this is not a problem as the system
        # is not to be evolved.
        if value < 0 and special_params.get('special') != 'CLASS':
            if self.boltzmann_order > 0 and (a > universals.a_begin or t > universals.t_begin):
                if t == -1:
                    t = cosmic_time(a)
                elif a == -1:
                    a = scale_factor(t)
                abort(
                    f'The equation of state parameter w for {self.name} took on the value '
                    f'{value} < 0 at t = {t} {unit_time}, a = {a}. '
                    f'This is disallowed for components with non-linear J.'
                )
        return value

    # Method for computing the effective equation of state parameter
    # w_eff at a certain time t or value of the scale factor a.
    # This has to be a pure Python function,
    # otherwise it cannot be called as w_eff(a=a).
    def w_eff(self, *, t=-1, a=-1):
        """This method should not be called before w_eff has been
        initialized by the initialize_w_eff method.
        """
        # For constant w_eff, w_eff = w
        if self.w_eff_type == 'constant':
            return self.w(t=t, a=a)
        elif self.w_eff_type == 'tabulated (a)':
            # If no time or scale factor value is passed,
            # use the current time and scale factor value.
            if t == -1 == a:
                t = universals.t
                a = universals.a
            # Compute w_eff
            if a == -1:
                a = scale_factor(t)
            return self.w_eff_spline.eval(a)
        abort(f'Did not recognize w_eff type "{self.w_eff_type}"')

    # Method for computing the proper time derivative
    # of the equation of state parameter w
    # at a certain time t or value of the scale factor a.
    # This has to be a pure Python function,
    # otherwise it cannot be called as ẇ(a=a).
    def ẇ(self, *, t=-1, a=-1):
        """This method should not be called before w has been
        initialized by the initialize_w method.
        """
        # If no time or scale factor value is passed,
        # use the current time and scale factor value.
        if t == -1 == a:
            t = universals.t
            a = universals.a
        # Compute ẇ dependent on its type
        if self.w_type == 'constant':
            return 0
        if self.w_type == 'tabulated (t)':
            if t == -1:
                t = cosmic_time(a)
            return self.w_spline.eval_deriv(t)
        if self.w_type == 'tabulated (a)':
            # Here we use dw/dt = da/dt*dw/da
            if a == -1:
                a = scale_factor(t)
            return ȧ(a)*self.w_spline.eval_deriv(a)
        if self.w_type == 'expression':
            # Approximate the derivative via symmetric difference
            if t == -1:
                t = cosmic_time(a)
            elif a == -1:
                a = scale_factor(t)
            Δx = 1e+6*machine_ϵ
            units_dict['t'] = t - Δx
            units_dict['a'] = a - Δx
            w_before = eval_unit(self.w_expression, units_dict)
            units_dict['t'] = t + Δx
            units_dict['a'] = a + Δx
            w_after = eval_unit(self.w_expression, units_dict)
            units_dict.pop('t')
            units_dict.pop('a')
            return (w_after - w_before)/(2*Δx)
        abort(f'Did not recognize w type "{self.w_type}"')

    # Method for computing the proper time derivative
    # of the effectice equation of state parameter w_eff
    # at a certain time t or value of the scale factor a.
    # This has to be a pure Python function,
    # otherwise it cannot be called as ẇ_eff(a=a).
    def ẇ_eff(self, *, t=-1, a=-1):
        """This method should not be called before w_eff has been
        initialized by the initialize_w_eff method.
        """
        # Compute ẇ_eff dependent on its type
        if self.w_eff_type == 'constant':
            return 0
        if self.w_eff_type == 'tabulated (a)':
            # If no time or scale factor value is passed,
            # use the current time and scale factor value.
            if t == -1 == a:
                t = universals.t
                a = universals.a
            # Compute ẇ_eff. Here we use dw_eff/dt = da/dt*dw_eff/da.
            if a == -1:
                a = scale_factor(t)
            return ȧ(a)*self.w_eff_spline.eval_deriv(a)
        abort(f'Did not recognize w_eff type "{self.w_type}"')

    # Method which initializes the equation of state parameter w.
    # Call this before calling the w and ẇ methods.
    @cython.header(
        # Arguments
        w=object,  # float-like, str or dict
        # Locals
        char=str,
        char_last=str,
        class_species=str,
        delim_left=str,
        delim_right=str,
        done_reading_w='bint',
        i='int',
        i_tabulated='double[:]',
        key=str,
        line=str,
        p_tabulated=object,  # np.ndarray
        pattern=str,
        unit='double',
        w_data='double[:, :]',
        w_list=list,
        w_ori=str,
        w_tabulated='double[:]',
        w_values='double[::1]',
        ρ_tabulated=object,  # np.ndarray
        returns='Spline',
    )
    def initialize_w(self, w):
        """The w argument can be one of the following (Python) types:
        - float-like: Designates a constant w.
                      The w will be stored in self.w_constant and
                      self.w_type will be set to 'constant'.
        - str       : If w is given as a str, it can mean any of
                      four things:
                      - w may be the string 'CLASS'.
                      - w may be the string 'default'.
                      - w may be a filename.
                      - w may be some analytical expression.
                      If w == 'CLASS', CLASS should be used to compute
                      w throughout time. The value of class_species
                      will be used to pick out w(a) for the correct
                      species. The result from CLASS are tabulated
                      w(a), where the a values will be stored as
                      self.w_tabulated[0, :], while the tabulated values
                      of w will be stored as self.w_tabulated[1, :].
                      The self.w_type will be set to 'tabulated (a)'.
                      If w == 'default', a constant w will be assigned
                      based on default_w and the species.
                      If w is a filename, the file should contain
                      tabulated values of w and either t or a. The file
                      must be in a format understood by np.loadtxt,
                      and a header must be present stating whether w(t)
                      or w(a) is tabulated. For w(t), the header should
                      also specify the units for the t column.
                      Legal formats for the header are:
                      # a    w(a)
                      # a    w
                      # t [Gyr]    w
                      # t [Gyr]    w(t)
                      In addition, the two columns may be specified in
                      the reverse order, parentheses and brackets may be
                      replaced with any of (), [], {}, <> and the number
                      of spaces/tabs does not matter.
                      The tabulated values for t or a will be stored as
                      self.w_tabulated as described above when using
                      CLASS, though self.w_type will be set to
                      'tabulated (t)' or 'tabulated (a)' depending on
                      whether t or a is used.
                      If w is some arbitrary expression,
                      this should include eather t or a.
                      The w will be stored in self.w_expression and
                      self.w_type will be set to 'expression'.
        - dict      : The dict has to be of the form
                      {'t': iterable, 'w': iterable} or
                      {'a': iterable, 'w': iterable}, where the
                      iterables are some iterables of matching
                      tabulated values.
                      The tabulated values for t or a will be stored as
                      self.w_tabulated[0, :], the tabulated values for w
                      as self.w_tabulated[1, :] and self.w_type will be
                      set to 'tabulated (t)' or 'tabulated (a)'.
        """
        # Initialize w dependent on its type
        try:
            w = float(w)
        except:
            pass
        if isinstance(w, float):
            # Assign passed constant w
            self.w_type = 'constant'
            self.w_constant = w
        elif isinstance(w, str) and w.lower() == 'class':
            # Get w as P_bar/ρ_bar from CLASS
            if not enable_class_background:
                abort(
                    f'Attempted to call CLASS to get the equation of state parameter w for the '
                    f'"{self.name}" component of CLASS species "{self.class_species}", '
                    f'but enable_class_background is False.'
                )
            self.w_type = 'tabulated (a)'
            cosmoresults = compute_cosmo(
                class_call_reason=f'in order to determine w(a) of {self.name} ',
            )
            background = cosmoresults.background
            i_tabulated = background['a']
            # For combination species it is still true that
            # w = c⁻²P_bar/ρ_bar, with P_bar and ρ_bar the sum of
            # individual background pressures and densities. Note that
            # the quantities in the background dict is given in CLASS
            # units, specifically c = 1.
            ρ_tabulated = 0
            p_tabulated = 0
            for class_species in self.class_species.split('+'):
                key = f'(.)rho_{class_species}'
                if key not in background:
                    abort(
                        f'No background density {key} for CLASS species "{class_species}" '
                        f'present in the CLASS background.'
                    )
                ρ_tabulated += background[key]
                key = f'(.)p_{class_species}'
                if key not in background:
                    abort(
                        f'No background pressure {key} for CLASS species "{class_species}" '
                        f'present in the CLASS background.'
                    )
                p_tabulated += background[key]
            w_tabulated = p_tabulated/ρ_tabulated
        elif isinstance(w, str) and w.lower() == 'default':
            # Assign w a constant value based on the species.
            # For combination species, the combined w is a weighted sum
            # of the invidual w's with the individual background
            # densities as weight, or equivalently, the ratio of the sum
            # of the invividual background pressures and the sum of the
            # individual background densities. To do it this proper way,
            # w should be passed in as 'class'. For w == 'default',
            # we simply do not handle this case.
            self.w_type = 'constant'
            w_constant = is_selected(self, default_w)
            try:
                self.w_constant = float(w_constant)
            except:
                abort(f'No default, constant w is defined for the "{self.species}" species')
        elif isinstance(w, str) and os.path.isfile(w):
            # Load tabulated w from file
            self.w_type = 'tabulated (?)'
            # Let only the master process read in the file
            if master:
                w_data = np.loadtxt(w)
                # Transpose w_data so that it consists of two rows
                w_data = asarray(w_data).T
                # For varying w it is crucial to know whether w is a
                # function of t or a.
                # This should be written in the header of the file.
                pattern = r'[a-zA-Z_]*\s*(?:\(\s*[a-zA-Z0-9\._]+\s*\))?'
                done_reading_w = False
                with open(w, 'r', encoding='utf-8') as w_file:
                    while True:
                        line = w_file.readline().lstrip()
                        if line and not line.startswith('#'):
                            break
                        line = line.replace('#', '').replace('\t', ' ').replace('\n', ' ')
                        for delim_left, delim_right in zip('[{<', ']}>'):
                            line = line.replace(delim_left , '(')
                            line = line.replace(delim_right, ')')
                        match = re.search(
                            r'\s*({pattern})\s+({pattern})\s*(.*)'.format(pattern=pattern),
                            line,
                        )
                        if match and not match.group(3):
                            # Header line containing the relevant
                            # information found.
                            for i in range(2):
                                var = match.group(1 + i)
                                if var[0] == 't':
                                    self.w_type = 'tabulated (t)'
                                    unit_match = re.search('\((.*)\)', var)  # Applied later
                                elif var[0] == 'a':
                                    self.w_type = 'tabulated (a)'
                                elif var[0] == 'w':
                                    # Extract the two rows
                                    w_tabulated = w_data[i, :]
                                    i_tabulated = w_data[(i + 1) % 2, :]
                                    done_reading_w = True
                        if done_reading_w and '(?)' not in self.w_type:
                            break
                # Multiply unit on the time
                if '(t)' in self.w_type:
                    if unit_match and unit_match.group(1) in units_dict:
                        unit = eval_unit(unit_match.group(1))
                        i_tabulated = asarray(i_tabulated)*unit
                    elif unit_match:
                        abort('Time unit "{}" in header of "{}" not understood'
                            .format(unit_match.group(1), w))
                    else:
                        abort('Could not find time unit in header of "{}"'.format(w))
        elif isinstance(w, str):
            # Some expression for w was passed.
            # Insert '*' between all numbers and letters as well as all
            # bewteen numbers and opening parentheses and between
            # closing parentheses and numbers/letters.
            w_ori = w
            w = w.lower().replace(' ', '').replace('_', '')
            w_list = []
            char_last = ''
            for char in w:
                if (
                    (
                        re.search(r'[0-9.]', char_last)
                    and (re.search(r'\w', char) and not re.search(r'[0-9.]', char))
                    )
                    or
                    (
                        re.search(r'[0-9.]', char)
                    and (re.search(r'\w', char_last) and not re.search(r'[0-9.]', char_last))
                    )
                    or
                    (
                        re.search(r'[0-9.]', char_last) and re.search(r'\(', char)
                    )

                    or
                    (
                        re.search(r'\)', char_last) and re.search(r'\w', char)
                    )
                ):
                    w_list.append('*')
                w_list.append(char)
                char_last = char
            w = ''.join(w_list)
            # Save the passed expression for w
            self.w_type = 'expression'
            self.w_expression = w
            # Test that the expression is parsable
            try:
                self.w()
            except:
                abort(
                    f'Cannot parse w = "{w_ori}" as an expression '
                    f'and no file with that name can be found either.'
                )
        elif isinstance(w, dict):
            # Use the tabulated w given by the two dict key-value pairs
            self.w_type = 'tabulated (?)'
            for key, val in w.items():
                if key.startswith('t'):
                    self.w_type = 'tabulated (t)'
                    i_tabulated = asarray(val, dtype=C2np['double'])
                elif key.startswith('a'):
                    self.w_type = 'tabulated (a)'
                    i_tabulated = asarray(val, dtype=C2np['double'])
                elif key.startswith('w'):
                    w_tabulated = asarray(val, dtype=C2np['double'])
        else:
            abort('Cannot handle w of type {}'.format(type(w)))
        # In the case of a tabulated w, only the master process
        # have the needed i_tabulated and w_tabulated. Broadcast these
        # to the slaves and instantiate a spline on all processes.
        if 'tabulated' in self.w_type:
            if master:
                # Check that tabulated values have been found
                if '(?)' in self.w_type:
                    abort(
                        'Could not detect the independent variable in tabulated '
                        'w data (should be \'a\' or \'t\')'
                    )
                # Make sure that the values of i_tabulated
                # are in increasing order.
                order = np.argsort(i_tabulated)
                i_tabulated = asarray(i_tabulated)[order]
                w_tabulated = asarray(w_tabulated)[order]
                # Pack the two rows together
                self.w_tabulated = empty((2, w_tabulated.shape[0]))
                self.w_tabulated[0, :] = i_tabulated
                self.w_tabulated[1, :] = w_tabulated
            # Broadcast the tabulated w
            self.w_type = bcast(self.w_type)
            self.w_tabulated = smart_mpi(self.w_tabulated if master else (), mpifun='bcast')
            # If the tabulated w is constant, treat it as such
            w_values = asarray(tuple(set(self.w_tabulated[1, :])))
            if isclose(min(w_values), max(w_values)):
                self.w_type = 'constant'
                self.w_constant = self.w_tabulated[1, 0]
            else:
                # Construct a Spline object from the tabulated data.
                # For most physical species, w(a) is approximately a
                # power law in a (and thus also approximately in t)
                # and so a log-log spline should be used.
                logx, logy = True, True
                if np.any(asarray(self.w_tabulated[1, :]) <= 0):
                    logy = False
                if self.class_species == 'fld':
                    # The CLASS dark energy fluid (fld) uses
                    # the {w_0, w_a} parameterization, and so a linear
                    # spline should be used.
                    logx, logy = False, False
                self.w_spline = Spline(self.w_tabulated[0, :], self.w_tabulated[1, :],
                    f'w{self.w_type[len(self.w_type) - 3:]} of {self.name}',
                    logx=logx, logy=logy)

    # Method which initializes the effective
    # equation of state parameter w_eff.
    # Call this before calling the w_eff method,
    # but after calling the initialize_w method.
    @cython.header(
        # Locals
        a='double',
        a_min='double',
        a_tabulated=object,  # np.ndarray
        integrand_spline='Spline',
        integrand_tabulated='double[::1]',
        t='double',
        t_tabulated='double[::1]',
        ρ_bar_tabulated=object,  # np.ndarray
    )
    def initialize_w_eff(self):
        """This method initializes the effective equation of state
        parameter w_eff by defining the w_eff_spline attribute,
        which is used by the w_eff method to get w_eff(a).
        Only future times compared to universals.a will be included.
        The definition of w_eff is
        ϱ = a**(3(1 + w_eff))ρ
        such that mean(ϱ) = ϱ_bar is constant in time. This leads to
        w_eff(a) = log(ρ_bar(a=1)/ρ_bar)/(3*log(a)) - 1,
        which is well behaved for any positive definite ρ_bar.
        """
        # If the CLASS background is disabled,
        # only constant w = w_eff is allowed.
        if not enable_class_background:
            # With the CLASS background disabled we do not have access
            # to the evolution of background densities. We can get by
            # as long as we have a constant w, in which case w_eff = w.
            # Note that this does not hold
            # for interacting/decaying species.
            if self.w_type != 'constant':
                abort(
                    f'Cannot construct w_eff of component "{self.name}" '
                    f'without access to the CLASS background evolution.'
                )
            self.w_eff_type = 'constant'
            return
        # Construct a tabulated array of scale factor values.
        # If w is already tabulated at certain values, reuse these.
        self.w_eff_type = 'tabulated (a)'
        if self.w_type == 'tabulated (a)':
            a_tabulated = self.w_tabulated[0, :]
            a_min = a_tabulated[0]
        elif self.w_type == 'tabulated (t)':
            t_tabulated = self.w_tabulated[0, :]
            a_tabulated = asarray([scale_factor(t) for t in t_tabulated])
            a_min = a_tabulated[0]
        else:
            a_min = -1
        cosmoresults = compute_cosmo(
            class_call_reason=f'in order to determine w_eff(a) of {self.name} ',
        )
        if a_min == -1:
            a_tabulated = cosmoresults.background['a']
        a_tabulated = a_tabulated.copy()  # Needed as we mutate a_tabulated below
        ρ_bar_tabulated = cosmoresults.ρ_bar(a_tabulated, self.class_species, apply_unit=False)
        ρ_bar_0 = ρ_bar_tabulated[ρ_bar_tabulated.shape[0] - 1]
        # At a = 1 a division by 0 error occurs due to 1/log(a).
        # Here we use linear extrapolation to obtain the end point.
        a_tabulated_end = a_tabulated[a_tabulated.shape[0] - 1]
        a_tabulated[a_tabulated.shape[0] - 1] = a_tabulated[a_tabulated.shape[0] - 2]
        w_eff_tabulated = np.log(ρ_bar_0/ρ_bar_tabulated)/(3*np.log(a_tabulated)) - 1
        if (  np.max(w_eff_tabulated[:w_eff_tabulated.shape[0] - 1])
            - np.min(w_eff_tabulated[:w_eff_tabulated.shape[0] - 1]) < 1e+6*machine_ϵ):
            # The effective equation of state is really constant.
            # Treat it as so.
            self.w_eff_type = 'constant'
            return
        a_tabulated[a_tabulated.shape[0] - 1] = a_tabulated_end
        # For most physical species, w_eff(a) is approximately a
        # power law in a and so log-log inter-/extrapolation
        # should be used.
        logx, logy = True, True
        if np.any(asarray(w_eff_tabulated[:w_eff_tabulated.shape[0] - 1]) <= 0):
            logy = False
        if self.class_species == 'fld':
            # The CLASS dark energy fluid (fld) uses
            # the {w_0, w_a} parameterization. It turns out that the
            # best spline is achieved from log(a) but linear w_eff.
            logx, logy = True, False
        # Extrapolate to get the value at a = 1
        w_eff_tabulated_end = scipy.interpolate.interp1d(
            np.log(     a_tabulated    [:a_tabulated.shape[0] - 1]) if logx else
                asarray(a_tabulated    [:a_tabulated.shape[0] - 1]),
            np.log(     w_eff_tabulated[:a_tabulated.shape[0] - 1]) if logy else
                asarray(w_eff_tabulated[:a_tabulated.shape[0] - 1]),
            'linear',
            fill_value='extrapolate',
        )(log(a_tabulated_end) if logx else a_tabulated_end)
        if logy:
            w_eff_tabulated_end = exp(w_eff_tabulated_end)
        w_eff_tabulated[a_tabulated.shape[0] - 1] = w_eff_tabulated_end
        # Instantiate the w_eff spline object
        self.w_eff_spline = Spline(a_tabulated, w_eff_tabulated, f'w_eff(a) of {self.name}',
            logx=logx, logy=logy)

    # Method which convert named fluid/particle
    # variable names to indices.
    @cython.header(# Arguments
                   varnames=object,  # str, int or container of str's and ints
                   single='bint',
                   # Locals
                   N_vars='Py_ssize_t',
                   i='Py_ssize_t',
                   indices='Py_ssize_t[::1]',
                   varname=object, # str or int
                   varnames_list=list,
                   returns=object,  # Py_ssize_t[::1] or Py_ssize_t
                   )
    def varnames2indices(self, varnames, single=False):
        """This method conveniently transform any reasonable input
        to an array of variable indices. Some examples:

        varnames2indices('ϱ') → asarray([0])
        varnames2indices(['J', 'ϱ']) → asarray([1, 0])
        varnames2indices(['pos', 'mom']) → asarray([0, 1])
        varnames2indices(2) → asarray([2])
        varnames2indices(['ς', 1]) → asarray([2, 1])
        varnames2indices('ϱ', single=True) → 0
        """
        if isinstance(varnames, str):
            # Single variable name given
            indices = asarray([self.fluid_names[varnames]], dtype=C2np['Py_ssize_t'])
        else:
            # One or more variable names/indices given
            varnames_list = any2list(varnames)
            indices = empty(len(varnames_list), dtype=C2np['Py_ssize_t'])
            for i, varname in enumerate(varnames_list):
                indices[i] = self.fluid_names[varname]
        if single:
            N_vars = indices.shape[0]
            if N_vars > 1:
                abort(f'The varnames2indices method was called '
                      'with {N_vars} variables while single is True')
            return indices[0]
        return indices

    # Method which extracts a fluid variable or fluidscalar from its
    # name, index or index and multi_index.
    def varname2var(self, varname):
        indices = self.fluid_names[varname]
        if isinstance(indices, tuple):
            # Return fluidscalar
            index, multi_indices = indices
            return self.fluidvars[index][multi_index]
        else:
            # Return fluid variable
            index = indices
            return self.fluidvars[index]

    # Generator for looping over all
    # scalar fluid grids within the component.
    def iterate_fluidscalars(self, include_disguised_scalar=True, include_additional_dofs=True):
        for i, fluidvar in enumerate(self.fluidvars):
            if include_disguised_scalar or i < self.boltzmann_order + 1:
                yield from fluidvar
                if include_additional_dofs:
                    for additional_dof in fluidvar.additional_dofs:
                        fluidscalar = fluidvar[additional_dof]
                        if fluidscalar is not None:
                            yield fluidscalar

    # Generator for looping over all non-linear
    # scalar fluid grids within the component.
    def iterate_nonlinear_fluidscalars(self):
        for fluidvar in self.fluidvars:
            for i, fluidscalar in enumerate(fluidvar):
                if fluidscalar is not None:
                    if i == 0 and fluidscalar.is_linear:
                        break
                    yield fluidscalar
            # Also yield the additional degrees of freedom
            # (e.g. 𝒫 corresponding to 'trace' on ς).
            for additional_dof in fluidvar.additional_dofs:
                fluidscalar = fluidvar[additional_dof]
                if fluidscalar is not None:
                    if fluidscalar.is_linear:
                        continue
                    yield fluidscalar

    # Method for communicating pseudo and ghost points
    # of all fluid variables.
    @cython.header(# Arguments
                   mode=str,
                   # Locals
                   fluidscalar='FluidScalar',
                   )
    def communicate_fluid_grids(self, mode=''):
        if self.representation != 'fluid':
            return
        for fluidscalar in self.iterate_fluidscalars():
            communicate_domain(fluidscalar.grid_mv, mode=mode)

    # Method for communicating pseudo and ghost points
    # of all starred fluid variables.
    @cython.header(# Arguments
                   mode=str,
                   # Locals
                   fluidscalar='FluidScalar',
                   )
    def communicate_fluid_gridsˣ(self, mode=''):
        if self.representation != 'fluid':
            return
        for fluidscalar in self.iterate_fluidscalars():
            communicate_domain(fluidscalar.gridˣ_mv, mode=mode)

    # Method for communicating pseudo and ghost points
    # of all non-linear fluid variables.
    @cython.header(# Arguments
                   mode=str,
                   # Locals
                   fluidscalar='FluidScalar',
                   )
    def communicate_nonlinear_fluid_grids(self, mode=''):
        if self.representation != 'fluid':
            return
        for fluidscalar in self.iterate_nonlinear_fluidscalars():
            communicate_domain(fluidscalar.grid_mv, mode=mode)

    # Method for communicating pseudo and ghost points
    # of all starred non-linear fluid variables.
    @cython.header(# Arguments
                   mode=str,
                   # Locals
                   fluidscalar='FluidScalar',
                   )
    def communicate_nonlinear_fluid_gridsˣ(self, mode=''):
        if self.representation != 'fluid':
            return
        for fluidscalar in self.iterate_nonlinear_fluidscalars():
            communicate_domain(fluidscalar.gridˣ_mv, mode=mode)

    # Method for communicating pseudo and ghost points
    # of all fluid Δ buffers.
    @cython.header(# Arguments
                   mode=str,
                   # Locals
                   fluidscalar='FluidScalar',
                   )
    def communicate_fluid_Δ(self, mode=''):
        if self.representation != 'fluid':
            return
        for fluidscalar in self.iterate_fluidscalars():
            communicate_domain(fluidscalar.Δ_mv, mode=mode)

    # Method which calls scale_grid on all non-linear fluid scalars
    @cython.header(# Arguments
                   a='double',
                   # Locals
                   fluidscalar='FluidScalar',
                   )
    def scale_nonlinear_fluid_grids(self, a):
        if self.representation != 'fluid':
            return
        for fluidscalar in self.iterate_nonlinear_fluidscalars():
            fluidscalar.scale_grid(a)

    # Method which calls the nullify_grid
    # on all non-linear fluid scalars.
    @cython.header(# Locals
                   fluidscalar='FluidScalar',
                   )
    def nullify_nonlinear_fluid_grids(self):
        if self.representation != 'fluid':
            return
        for fluidscalar in self.iterate_nonlinear_fluidscalars():
            fluidscalar.nullify_grid()

    # Method which calls the nullify_gridˣ
    # on all non-linear fluid scalars.
    @cython.header(# Locals
                   fluidscalar='FluidScalar',
                   )
    def nullify_nonlinear_fluid_gridsˣ(self):
        if self.representation != 'fluid':
            return
        for fluidscalar in self.iterate_nonlinear_fluidscalars():
            fluidscalar.nullify_gridˣ()

    # Method which calls the nullify_Δ on all fluid scalars
    @cython.header(# Arguments
                   specifically=object,  # str og container of str's
                   # Locals
                   fluidscalar='FluidScalar',
                   )
    def nullify_Δ(self, specifically=None):
        if self.representation == 'particles':
            if specifically is None:
                self.Δposx_mv[...] = 0
                self.Δposy_mv[...] = 0
                self.Δposz_mv[...] = 0
                self.Δmomx_mv[...] = 0
                self.Δmomy_mv[...] = 0
                self.Δmomz_mv[...] = 0
            else:
                if 'pos' in specifically:
                    self.Δposx_mv[...] = 0
                    self.Δposy_mv[...] = 0
                    self.Δposz_mv[...] = 0
                if 'mom' in specifically:
                    self.Δmomx_mv[...] = 0
                    self.Δmomy_mv[...] = 0
                    self.Δmomz_mv[...] = 0
        elif self.representation == 'fluid':
            for fluidscalar in self.iterate_fluidscalars():
                fluidscalar.nullify_Δ()

    # Method which copies the content of all unstarred non-linear grids
    # into the corresponding starred grids.
    @cython.header(fluidscalar='FluidScalar', operation=str)
    def copy_nonlinear_fluid_grids_to_gridsˣ(self, operation='='):
        if self.representation != 'fluid':
            return
        for fluidscalar in self.iterate_nonlinear_fluidscalars():
            fluidscalar.copy_grid_to_gridˣ(operation)

    # Method which copies the content of all starred non-linear grids
    # into the corresponding unstarred grids.
    @cython.header(fluidscalar='FluidScalar', operation=str)
    def copy_nonlinear_fluid_gridsˣ_to_grids(self, operation='='):
        if self.representation != 'fluid':
            return
        for fluidscalar in self.iterate_nonlinear_fluidscalars():
            fluidscalar.copy_gridˣ_to_grid(operation)

    # This method is automaticlly called when a Component instance
    # is garbage collected. All manually allocated memory is freed.
    def __dealloc__(self):
        # Free particle data
        # (fluid data lives on FluidScalar instances).
        free(self.pos)
        free(self.posx)
        free(self.posy)
        free(self.posz)
        free(self.mom)
        free(self.momx)
        free(self.momy)
        free(self.momz)
        free(self.Δpos)
        free(self.Δposx)
        free(self.Δposy)
        free(self.Δposz)
        free(self.Δmom)
        free(self.Δmomx)
        free(self.Δmomy)
        free(self.Δmomz)
        free(self.rungs_N)
        free(self.rung_indices)
        free(self.rung_jumps)

    # String representation
    def __repr__(self):
        return '<component "{}" of species "{}">'.format(self.name, self.species)
    def __str__(self):
        return self.__repr__()

# Array used by the Component.tile_sort() method
cython.declare(tile_location='double[::1]')
tile_location = empty(3, dtype=C2np['double'])



# Function for getting the component representation based on the species
@cython.header(# Arguments
               species=str,
               # Locals
               key=tuple,
               representation=str,
               returns=str
               )
def get_representation(species):
    for key, representation in representation_of_species.items():
        if species in key:
            return representation
    abort('Species "{}" not implemented'.format(species))

# Function for adding species to the universals_dict,
# recording the presence of any species in use.
@cython.header(
    # Arguments
    components=list,
    # Locals
    class_species_present=set,
    class_species_present_bytes=bytes,
    class_species_previously_present=str,
    i='Py_ssize_t',
    species_present=set,
    species_present_bytes=bytes,
    species_previously_present=str,
)
def update_species_present(components):
    """We cannot change the species_present and class_species_present
    fields of the universals structure, as this would require sticking
    in a new char*. Instead we use the universals_dict dict.
    For consistency, we store the species as bytes objects.
    """
    components = components.copy()
    for i, component in enumerate(components):
        if component.name in internally_defined_names:
            components[i] = None
    components = [component for component in components if component is not None]
    if not components:
        return
    # Species present (CO𝘕CEPT convention)
    species_present = {component.species for component in components}
    species_previously_present = universals_dict['species_present'].decode()
    if species_previously_present:
        species_present |= set(species_previously_present.split('+'))
    species_present_bytes = '+'.join(species_present).encode()
    universals_dict['species_present'] = species_present_bytes
    # Species present (CLASS convention)
    class_species_present = {component.class_species for component in components}
    class_species_previously_present = universals_dict['class_species_present'].decode()
    if class_species_previously_present:
        class_species_present |= set(class_species_previously_present.split('+'))
    class_species_present_bytes = (
        # A component.class_species may be a combination of several CLASS species
        '+'.join(set('+'.join(class_species_present).split('+'))).encode()
    )
    universals_dict['class_species_present'] = class_species_present_bytes

# Function which refines the subtiling corresponding to the given
# interaction_name, on all instantiated components. The original
# subtilings are not deleted, and so may later be substituted back in
# if desired.
@cython.header(
    # Arguments
    interaction_name=str,
    # Locals
    component='Component',
    computation_time_total='double',
    dim='int',
    key=tuple,
    key2=tuple,
    refinement_offset='Py_ssize_t',
    shape='Py_ssize_t[::1]',
    subtile_extent='double[::1]',
    subtile_extent_max='double',
    subtiling='Tiling',
    subtiling_2='Tiling',
    subtiling_name=str,
    subtiling_name_2=str,
    subtiling_rejected='Tiling',
    subtiling_rejected_2='Tiling',
    returns='void',
)
def tentatively_refine_subtiling(interaction_name):
    subtiling_name   = f'{interaction_name} (subtiles)'
    subtiling_name_2 = f'{interaction_name} (subtiles 2)'
    shape = None
    for component in components_all:
        subtiling = component.tilings.pop(subtiling_name, None)
        if subtiling is None:
            continue
        # Component with the right subtiling found.
        # Take a copy of its computation_time_total and
        # refinement_offset attributes, as we need to copy these over
        # to the refined subtiling.
        computation_time_total = subtiling.computation_time_total
        refinement_offset      = subtiling.refinement_offset
        # Remove the subtiling (including its second version
        # "subtiles 2") from the component and store it away.
        key = (component, subtiling_name)
        stored_subtilings[key] = subtiling
        subtiling_2 = component.tilings.pop(subtiling_name_2, None)
        if subtiling_2 is not None:
            key2 = (component, subtiling_name_2)
            stored_subtilings[key2] = subtiling_2
        # If we have already attempted this refinement before,
        # the new, rejected subtilings are stored
        # in rejected_subtilings. Reuse these if available.
        subtiling_rejected = rejected_subtilings.pop(key, None)
        if subtiling_rejected is not None:
            subtiling_rejected.computation_time_total = computation_time_total
            subtiling_rejected.refinement_offset      = refinement_offset
            component.tilings[subtiling_name] = subtiling_rejected
            if subtiling_2 is not None:
                subtiling_rejected_2 = rejected_subtilings.pop(key2, None)
                if subtiling_rejected_2 is not None:
                    component.tilings[subtiling_name_2] = subtiling_rejected_2
            tiling_shapes[subtiling_name] = asarray(subtiling_rejected.shape).copy()
            continue
        # Refine the subtiling shape
        if shape is None:
            shape = tiling_shapes[subtiling_name]
            subtile_extent = subtiling.tile_extent
            subtile_extent_max = max(subtile_extent)
            for dim in range(3):
                if subtile_extent[dim] == subtile_extent_max:
                    shape[dim] += 1
        # Initialize new subtiling
        subtiling = component.init_tiling(subtiling_name)
        subtiling.computation_time_total = computation_time_total
        subtiling.refinement_offset      = refinement_offset
        if subtiling_2 is not None:
            component.tilings.pop(subtiling_name)
            subtiling_2 = component.init_tiling(subtiling_name)
            component.tilings[subtiling_name  ] = subtiling
            component.tilings[subtiling_name_2] = subtiling_2
# Global containers temporarily storing subtilings for each component
cython.declare(stored_subtilings=dict, rejected_subtilings=dict)
stored_subtilings = {}
rejected_subtilings = {}

# Function which either accepts or rejects the tentative subtile
# refining carried out by tentatively_refine_subtiling().
@cython.header(
    # Arguments
    interaction_name=str,
    computation_times_sum='double[::1]',
    computation_times_sqsums='double[::1]',
    computation_times_N='Py_ssize_t[::1]',
    # Locals
    N='Py_ssize_t',
    any_acceptance='bint',
    component='Component',
    computation_time_total_new='double',
    computation_time_total_old='double',
    index='Py_ssize_t',
    key=tuple,
    keys=list,
    message='Py_ssize_t[::1]',
    name=str,
    other_rank='int',
    sigmas='double',
    shape='Py_ssize_t[::1]',
    subtiling='Tiling',
    subtiling_name=str,
    subtiling_name_2=str,
    subtiling_names=set,
    subtiling_rejected='Tiling',
    returns='void',
)
def accept_or_reject_subtiling_refinement(
    interaction_name, computation_times_sum, computation_times_sqsums, computation_times_N,
):
    """
    All three computation_times_* arrays are indexed as follows:
    computation_times_sum[rung_index] -> new computation times with
    lowest_active_rung == rung_index, "new" meaning with the subtiling
    refinement.
    computation_times_sum[N_rungs + rung_index] -> old computation
    times with, "old" meaning with the original subtiling refinement.
    """
    # Compute mean of all computation times
    for index in range(ℤ[2*N_rungs]):
        N = computation_times_N[index]
        computation_times_mean[index] = (
            computation_times_sum[index]/computation_times_N[index]
        ) if N > 0 else 0
    # Compute std of old computation times
    for index in range(N_rungs, ℤ[2*N_rungs]):
        N = computation_times_N[index]
        computation_times_std[index] = (
            sqrt(computation_times_sqsums[index]/N - computation_times_mean[index]**2)
        ) if N > 0 else 0
    # Compute the total time it took to carry out all of the old
    # computations together. To exaggerate a bit (encouraging early
    # subtile refinement), we use mean + sigmas*std rather than just
    # the mean, where sigmas sets the number of standard deviations
    # we wish to exaggerate with.
    sigmas = 0.3
    computation_time_total_old = 0
    for index in range(N_rungs, ℤ[2*N_rungs]):
        if computation_times_N[index] == 0 or computation_times_N[-N_rungs + index] == 0:
            continue
        computation_time_total_old += computation_times_N[index]*(
            computation_times_mean[index] + sigmas*computation_times_std[index]
        )
    # Compute the total time it would take the new subtiling to perform
    # the work recorded by the old subtiling.
    computation_time_total_new = 0
    for index in range(N_rungs):
        if computation_times_N[index] == 0 or computation_times_N[N_rungs + index] == 0:
            continue
        computation_time_total_new += (
            computation_times_N[N_rungs + index]*computation_times_mean[index]
        )
    # Accept the recent subtiling refinement if it outperforms the
    # old one. Otherwise reject it.
    subtiling_name   = f'{interaction_name} (subtiles)'
    subtiling_name_2 = f'{interaction_name} (subtiles 2)'
    subtiling_names = {subtiling_name, subtiling_name_2}
    if computation_time_total_new < computation_time_total_old:
        # Accept recent subtiling refinement.
        # The old subtilings should be cleaned up. Here we remove the
        # only references to the old subtilings, after which their
        # memory is available for garbage collection.
        for key in tuple(stored_subtilings.keys()):
            if key[1] in subtiling_names:
                stored_subtilings.pop(key)
        # We want to send the new subtiling decomposition
        # to the master processes.
        message = tiling_shapes[subtiling_name]
    else:
        # Reject recent subtiling refinement.
        # Move the stored subtilings back on to their respective
        # components and insert rejected subtilings into the global
        # rejected_subtilings dict.
        keys = []
        for key, subtiling in stored_subtilings.items():
            component, name = key
            if name in subtiling_names:
                keys.append(key)
                shape = subtiling.shape
                subtiling_rejected = component.tilings[name]
                subtiling.computation_time_total = subtiling_rejected.computation_time_total
                subtiling.refinement_offset      = subtiling_rejected.refinement_offset
                rejected_subtilings[key] = subtiling_rejected
                component.tilings[name] = subtiling
        tiling_shapes[subtiling_name] = asarray(shape).copy()
        # Remove the old references in the stored_subtilings dict
        for key in keys:
            stored_subtilings.pop(key)
        # We want to let the master process know that the new subtiling
        # has been rejected.
        message = subtiling_refinement_message_rejected
    # Gather information about the acceptance of the new subtiling
    # and print out any positive results.
    Gather(message, subtiling_refinement_message_recv)
    any_acceptance = False
    if master:
        for other_rank in range(nprocs):
            index = 3*other_rank
            if subtiling_refinement_message_recv[index] == 0:
                continue
            any_acceptance = True
            shape = subtiling_refinement_message_recv[index:index+3]
            with unswitch:
                if interaction_name == 'gravity':
                    masterprint(
                        f'Rank {other_rank}: Refined gravitational subtile decomposition: '
                        f'{shape[0]}×{shape[1]}×{shape[2]}'
                    )
                elif ...:
                    ...
    # If even a single process accepted the refinement, we fast forward
    # the refinement cycle so that we immeiately begin collecting
    # computation times, on all processes.
    if bcast(any_acceptance if master else None):
        for component in components_all:
            subtiling = component.tilings.get(subtiling_name)
            if subtiling is None:
                continue
            subtiling.refinement_offset += (
                subtiling.refinement_period - subtiling_refinement_period_min
            )
# Arrays used by the accept_or_reject_subtiling_refinement() function
cython.declare(
    computation_times_mean='double[::1]',
    computation_times_std='double[::1]',
    subtiling_refinement_message_rejected='Py_ssize_t[::1]',
    subtiling_refinement_message_recv='Py_ssize_t[::1]',
)
computation_times_mean = zeros(2*N_rungs, dtype=C2np['double'])
computation_times_std  = zeros(2*N_rungs, dtype=C2np['double'])
subtiling_refinement_message_rejected = zeros(3, dtype=C2np['Py_ssize_t'])
subtiling_refinement_message_recv = empty(3*nprocs, dtype=C2np['Py_ssize_t']) if master else None



# Mapping from tiling names to shapes of all tilings instantiated
# across all components.
cython.declare(tiling_shapes=dict)
tiling_shapes = {}

# Mapping from species to their representations
cython.declare(representation_of_species=dict)
representation_of_species = {
    (
        'baryons',
        'dark energy particles',
        'dark matter particles',
        'decay radiation particles',
        'decaying dark matter particles',
        'matter particles',
        'neutrinos',
        'photons',
        'particles',
    ): 'particles',
    (
        'baryon fluid',
        'dark energy fluid',
        'dark matter fluid',
        'decay radiation fluid',
        'decaying dark matter fluid',
        'lapse',
        'matter fluid',
        'metric',
        'neutrino fluid',
        'photon fluid',
        'fluid',
    ): 'fluid',
}
# Mapping from (CO𝘕CEPT) species names to default
# CLASS species names. Note that combination species
# (e.g. matter) is expressed as e.g. 'cdm+b'.
cython.declare(default_class_species=dict)
default_class_species = {
    'baryon fluid'                  : 'b',
    'baryons'                       : 'b',
    'dark energy fluid'             : 'fld',
    'dark energy particles'         : 'fld',
    'dark matter fluid'             : 'cdm',
    'dark matter particles'         : 'cdm',
    'decay radiation fluid'         : 'dr',
    'decay radiation particles'     : 'dr',
    'decaying dark matter fluid'    : 'dcdm',
    'decaying dark matter particles': 'dcdm',
    'lapse'                         : 'lapse',
    'matter fluid'                  : matter_class_species,
    'matter particles'              : matter_class_species,
    'metric'                        : 'metric',
    'neutrino fluid'                : 'ncdm[0]',
    'neutrinos'                     : 'ncdm[0]',
    'photon fluid'                  : 'g',
    'photons'                       : 'g',
}
# Mapping from species and representations to default w values
cython.declare(default_w=dict)
default_w = {
    'baryon fluid'                  :  0,
    'baryons'                       :  0,
    'dark energy fluid'             : -1,
    'dark energy particles'         : -1,
    'dark matter fluid'             :  0,
    'dark matter particles'         :  0,
    'decay radiation fluid'         :  1/3,
    'decay radiation particles'     :  1/3,
    'decaying dark matter fluid'    :  0,
    'decaying dark matter particles':  0,
    'lapse'                         :  0,
    'matter fluid'                  :  0,
    'matter particles'              :  0,
    'metric'                        :  0,
    'neutrino fluid'                :  1/3,
    'neutrinos'                     :  1/3,
    'photons'                       :  1/3,
    'photon fluid'                  :  1/3,
}
# Set of all approximations implemented on Component objects
cython.declare(approximations_implemented=set)
approximations_implemented = {
    unicode('P=wρ'),
}
# Set of all component names used internally by the code,
# and which the user should generally avoid.
cython.declare(internally_defined_names=set)
internally_defined_names = {'all', 'all combinations', 'buffer', 'default', 'tmp', 'total', 'fake'}
# Names of all implemented fluid variables in order.
# Note that 𝒫 is not considered a seperate fluid variable,
# but rather a fluid scalar that lives on ς.
cython.declare(fluidvar_names=tuple)
fluidvar_names = ('ϱ', 'J', 'ς')
# Flag specifying whether a warning should be given if multiple
# components with the same name are instantiated, and a set of names of
# all instantiated componenets.
cython.declare(allow_similarly_named_components='bint', component_names=set)
allow_similarly_named_components = False
component_names = set()
# Set of all instantiated components
cython.declare(components_all=set)
components_all = set()