from datetime import datetime
from functools import lru_cache
import inspect
import os
import shutil
import subprocess

import numpy as np

from utils.conversion import units_convert, t2s, s2t, dt1h
from config import parse_config

from . import restart
from . import forcing

class Model(object):

    def __init__(self, config_file=None, parse_args=False, **kwargs):

        ##parse config file and obtain a list of attributes
        code_dir = os.path.dirname(inspect.getfile(self.__class__))
        config_dict = parse_config(code_dir, config_file, parse_args, **kwargs)
        for key, value in config_dict.items():
            setattr(self, key, value)

        levels = np.arange(1, 51, 1)
        level_sfc = np.array([0])
        self.variables = {
                'ocean_velocity': {'name':('u', 'v'), 'dtype':'float', 'is_vector':True, 'levels':levels, 'units':'m/s'},
                'ocean_layer_thick': {'name':'dp', 'dtype':'float', 'is_vector':False, 'levels':levels, 'units':'Pa'},
                'ocean_temp': {'name':'temp', 'dtype':'float', 'is_vector':False, 'levels':levels, 'units':'K'},
                'ocean_saln': {'name':'saln', 'dtype':'float', 'is_vector':False, 'levels':levels, 'units':'psu'},
                'ocean_surf_height': {'name':'msshb', 'dtype':'float', 'is_vector':False, 'levels':[0], 'units':'m'},
                'ocean_surf_temp': {'name':'sstb', 'dtype':'float', 'is_vector':False, 'levels':[0], 'units':'K'},
                'ocean_b_velocity':  {'name':('ubavg', 'vbavg'), 'dtype':'float', 'is_vector':True, 'levels':level_sfc, 'units':'m/s'},
                'ocean_b_press': {'name':'pbavg', 'dtype':'float', 'is_vector':False, 'levels':level_sfc, 'units':'Pa'},
                'ocean_mixl_depth': {'name':'dpmixl', 'dtype':'float', 'is_vector':False, 'levels':level_sfc, 'units':'Pa'},
                'seaice_velocity': {'name':('uice', 'vice'), 'dtype':'float', 'is_vector':True, 'levels':level_sfc, 'units':'m/s'},
                'seaice_conc': {'name':'ficem', 'dtype':'float', 'is_vector':False, 'levels':level_sfc, 'units':'%'},
                'seaice_thick': {'name':'hicem', 'dtype':'float', 'is_vector':False, 'levels':level_sfc, 'units':'m'},
                }
        self.z_units = 'm'
#        self.read_grid()

        self.run_process = None
        self.run_status = 'pending'
        self.grid=''



    def filename(self, **kwargs):
        if 'path' in kwargs:
            path = kwargs['path']
        else:
            path = '.'
        if 'time' in kwargs and kwargs['time'] is not None:
            assert isinstance(kwargs['time'], datetime), 'time shall be a datetime object'
            tstr = kwargs['time'].strftime('%Y_%j_%H')
        else:
            tstr = '????_???_??'
        if 'member' in kwargs and kwargs['member'] is not None:
            assert kwargs['member'] >= 0, 'member index shall be >= 0'
            mstr = '_mem{:03d}'.format(kwargs['member']+1)
            mdir = '{:03d}'.format(kwargs['member']+1)
        else:
            mstr = ''
            mdir = ''
        return os.path.join(path, mdir, 'TP4restart'+tstr+mstr+'.a')


    def read_grid(self, **kwargs):
        pass


    def write_grid(self, **kwargs):
        pass


    def read_mask(self):
        pass


    def read_var(self, **kwargs):
        ##check name in kwargs and read the variables from file
        assert 'name' in kwargs, 'please specify which variable to get, name=?'
        name = kwargs['name']
        assert name in variables, 'variable name '+name+' not listed in variables'
        fname = filename(path, **kwargs)

        if 'k' in kwargs:
            ##Note: ocean level indices are negative in assim_tools.state
            ##      but in abfiles, they are defined as positive indices
            k = -kwargs['k']
        else:
            k = -variables[name]['levels'][-1]  ##get the first level if not specified
        if 'mask' in kwargs:
            mask = kwargs['mask']
        else:
            mask = None

        if 'is_vector' in kwargs:
            is_vector = kwargs['is_vector']
        else:
            is_vector = variables[name]['is_vector']

        if 'units' in kwargs:
            units = kwargs['units']
        else:
            units = variables[name]['units']

        var = units_convert(units, variables[name]['units'], var)
        return var


    def write_var(path, grid, var, **kwargs):
        ##check name in kwargs
        assert 'name' in kwargs, 'please specify which variable to write, name=?'
        name = kwargs['name']
        assert name in variables, 'variable name '+name+' not listed in variables'
        fname = filename(path, **kwargs)

        ##same logic for setting level indices as in read_var()
        if 'k' in kwargs:
            k = -kwargs['k']
        else:
            k = -variables[name]['levels'][-1]
        if 'mask' in kwargs:
            mask = kwargs['mask']
        else:
            mask = None

        ##open the restart file for over-writing
        ##the 'r+' mode and a new overwrite_field method were added in the ABFileRestart in .abfile


    @lru_cache(maxsize=3)
    def z_coords(self, **kwargs):
        """calculate vertical coordinates given the 3D model state
        """
        ##check if level is provided
        assert 'k' in kwargs, 'missing level index in kwargs for z_coords calc, level=?'

        z = np.zeros(grid.x.shape)
        if kwargs['k'] == 0:
            ##if level index is 0, this is the surface, so just return zeros
            return z
        else:
            ##get layer thickness above level k, convert to z_units, and integrate to total depth
            for k in [k for k in levels if k>=kwargs['k']]:
                rec = kwargs.copy()
                rec['name'] = 'ocean_layer_thick'
                rec['units'] = variables['ocean_layer_thick']['units']
                rec['k'] = k
                d = read_var(path, grid, **rec)
                if kwargs['units'] == 'm':
                    onem = 9806.
                    z -= d/onem  ##accumulate depth in meters, negative relative to surface
                else:
                    raise ValueError('do not know how to calculate z_coords for z_units = '+kwargs['units'])
            return z


    def generate_initial_condition(self, task_id:int=0, task_nproc:int=1, **kwargs):
        """generate initial condition for a single ensemble member.

        Parameters
        ----------
        task_id : int
            task id for parallel execution
        task_nproc : int
            number of processors for each task
        **kwargs : dict
            keyword arguments for the model configuration
            Keywords defined when the function is called:
            - member : int
                ensemble member id
            These are defined in the configuration file of model_def/nextsim.dg section
            - ens_init_dir : str
                directory storing the initial ensemble
            - restart : dict
                restart file options.
                See example configuration file for required keys and explanations.
                This section is not necessary if the model does not use restart files.
            - perturb : dict
                perturbation options for the initial conditions.
                See example configuration file for required keys and explanations.
                This section is not necessary if the model does not use perturbation
                for the initial conditions or forcings.

        """
        # get the current ensemble member id
        ens_mem_id = kwargs['member']
        # ensemble member directory for the current member
        ens_init_dir = os.path.join(kwargs['ens_init_dir'],
                                    f'ens_{str(ens_mem_id).zfill(2)}')
        # create directory for the initial ensemble
        os.makedirs(ens_init_dir, exist_ok=True)
        # get all required filenames for the initial ensemble
        # 1. get current time, which is supposed to be the start time
        time = kwargs['time_start']
        # 2. get the restart filename
        try:
            file_options_restart = kwargs['files']['restart']
            fname_restart:str = restart.get_restart_filename(file_options_restart, ens_mem_id, time)
        except KeyError:
            print ('restart file is not specified in model configuration.'
                   ' We do not use restart files.')
        # 3. get the forcing filenames
        file_options_forcing = kwargs['files']['forcing']
        fname_forcing:dict[str, str] = dict()
        for forcing_name in file_options_forcing.keys():
            fname_forcing[forcing_name] = forcing.get_forcing_filename(file_options_forcing[forcing_name],
                                                         ens_mem_id, time)
        # add perturbations
        try:
            perturb_options = kwargs['perturb']
            # if we have a perturbation for the restart file
            try:
                restart_options = perturb_options['restart']
                # copy restart files to the ensemble member directory
                fname = os.path.join(ens_init_dir, os.path.basename(fname_restart))
                if not os.path.exists(fname):
                    shutil.copy(fname_restart, ens_init_dir)
                # prepare the restart file options for the perturbation
                file_options = {'fname': fname,
                               'lon_name':file_options_restart['lon_name'],
                               'lat_name':file_options_restart['lat_name']}
                # perturb the restart file
                restart.perturb_restart(restart_options, file_options, ens_mem_id, time)
            except KeyError:
                # we we do not perturb the restart file
                # simply link the restart files
                os.system(f'ln -s {fname_restart} {ens_init_dir}')
            # if we have a perturbation for the forcing
            try:
                forcing_options = perturb_options['forcing']
                file_options:dict = dict()
                for forcing_name in forcing_options.keys():
                    # we ignore entries that are not in the files options
                    # e.g., path
                    if forcing_name not in fname_forcing: continue
                    # copy forcing files to the ensemble member directory
                    fname = os.path.join(ens_init_dir,
                                         os.path.basename(fname_forcing[forcing_name])
                                         )
                    if not os.path.exists(fname):
                        shutil.copy(fname_forcing[forcing_name], ens_init_dir)
                    # the forcing file options for the perturbation
                    file_options[forcing_name] = {'fname': fname,
                                                   **file_options_forcing[forcing_name]}
                forcing.perturb_forcing(forcing_options, file_options, ens_mem_id, time, time)
            except KeyError:
                # we we do not perturb the forcing file
                # simply link the forcing files
                for forcing_name in forcing_options.keys():
                    os.system(f'ln -s {fname_forcing[forcing_name]} {ens_init_dir}')
        except KeyError:
            print ('We do no perturbations as perturb section is not specified in the model configuration.')


    def run(self, task_id=0, task_nproc=256, **kwargs):
        """run the model for a single ensemble member.
        """
        # get the current ensemble member id
        ens_mem_id = kwargs['member']
        # ensemble member directory for the current member
        ens_init_dir = os.path.join(kwargs['ens_init_dir'],
                                    f'ens_{str(ens_mem_id).zfill(2)}')
        # create directory for the initial ensemble
        os.makedirs(ens_init_dir, exist_ok=True)
        time = kwargs['time']
        forecast_period = kwargs['forecast_period']
        next_time = time + forecast_period * dt1h
        prev_time = time - forecast_period * dt1h
        # preparing the forcing perturbation
        # 1. get the forcing filenames
        file_options_forcing = kwargs['files']['forcing']
        fname_forcing:dict[str, str] = dict()
        for forcing_name in file_options_forcing.keys():
            fname_forcing[forcing_name] = forcing.get_forcing_filename(file_options_forcing[forcing_name],
                                                         ens_mem_id, time)
        # add perturbations
        try:
            perturb_options = kwargs['perturb']
            # if we have a perturbation for the forcing
            try:
                forcing_options = perturb_options['forcing']
                file_options:dict = dict()
                for forcing_name in forcing_options.keys():
                    # we ignore entries that are not in the files options
                    # e.g., path
                    if forcing_name not in fname_forcing: continue
                    # copy forcing files to the ensemble member directory
                    fname = os.path.join(ens_init_dir,
                                         os.path.basename(fname_forcing[forcing_name])
                                         )
                    if not os.path.exists(fname):
                        shutil.copy(fname_forcing[forcing_name], ens_init_dir)
                    # the forcing file options for the perturbation
                    file_options[forcing_name] = {'fname': fname,
                                                   **file_options_forcing[forcing_name]}
                forcing.perturb_forcing(forcing_options, file_options, ens_mem_id, time, prev_time)
            except KeyError:
                # we we do not perturb the forcing file
                # simply link the forcing files
                for forcing_name in forcing_options.keys():
                    os.system(f'ln -s {fname_forcing[forcing_name]} {ens_init_dir}')
        except KeyError:
            print ('We do no perturbations as perturb section is not specified in the model configuration.')

        # run the model, i.e., calling the run.sh script


if __name__ == '__main__':
    pass
