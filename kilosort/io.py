import json
from pathlib import Path
from typing import Tuple, Union
import os, shutil
import warnings

from scipy.io import loadmat
import numpy as np
import torch
from torch.fft import fft, ifft, fftshift

from kilosort import CCG
from kilosort.preprocessing import get_drift_matrix, fft_highpass


def find_binary(data_dir: Union[str, os.PathLike]) -> Path:
    """Find binary file in `data_dir`."""

    data_dir = Path(data_dir)
    filenames = list(data_dir.glob('*.bin')) + list(data_dir.glob('*.bat')) \
                + list(data_dir.glob('*.dat'))
    if len(filenames) == 0:
        raise FileNotFoundError('No binary file (*.bin or *.bat) found in folder')

    # TODO: Why give this preference? Not all binary files will have this tag.
    # If there are multiple binary files, find one with "ap" tag
    if len(filenames) > 1:
        filenames = [f for f in filenames if 'ap' in f.as_posix()]

    # If there is still more than one, raise an error, user needs to specify
    # full path.
    if len(filenames) > 1:
        raise ValueError('Multiple binary files in folder with "ap" tag, '
                         'please specify filename')

    return filenames[0]


def load_probe(probe_path):
    """Load a .mat probe file from Kilosort2, or a PRB file and returns a dictionary
    
    adapted from https://github.com/MouseLand/pykilosort/blob/5712cfd2722a20554fa5077dd8699f68508d1b1a/pykilosort/utils.py#L592

    """
    probe = {}
    probe_path = Path(probe_path).resolve()
    required_keys = ['chanMap', 'yc', 'xc', 'n_chan']

    if probe_path.suffix == '.prb':
        # Support for PRB files.
        # !DOES NOT WORK FOR PHASE3A PROBES WITH DISCONNECTED CHANNELS!
        # Also does not remove reference channel in PHASE3B probes
        contents = probe_path.read_text()
        metadata = {}
        exec(contents, {}, metadata)
        probe['chanMap'] = []
        probe['xc'] = []
        probe['yc'] = []
        probe['kcoords'] = []
        probe['n_chan'] = 0 
        for cg in sorted(metadata['channel_groups']):
            d = metadata['channel_groups'][cg]
            ch = d['channels']
            pos = d.get('geometry', {})
            probe['chanMap'].append(ch)
            probe['n_chan'] += len(ch)
            probe['xc'].append([pos[c][0] for c in ch])
            probe['yc'].append([pos[c][1] for c in ch])
            probe['kcoords'].append([cg for c in ch])
        probe['chanMap'] = np.concatenate(probe['chanMap']).ravel().astype(np.int32)
        probe['xc'] = np.concatenate(probe['xc']).astype('float32')
        probe['yc'] = np.concatenate(probe['yc']).astype('float32')
        probe['kcoords'] = np.concatenate(probe['kcoords']).astype('float32')

    elif probe_path.suffix == '.mat':
        mat = loadmat(probe_path)
        connected = mat['connected'].ravel().astype('bool')
        probe['xc'] = mat['xcoords'].ravel().astype(np.float32)[connected]
        nc = len(probe['xc'])
        probe['yc'] = mat['ycoords'].ravel().astype(np.float32)[connected]
        probe['kcoords'] = mat.get('kcoords', np.zeros(nc)).ravel().astype(np.float32)
        probe['chanMap'] = (mat['chanMap'] - 1).ravel().astype(np.int32)[connected]  # NOTE: 0-indexing in Python
        probe['n_chan'] = (mat['chanMap'] - 1).ravel().astype(np.int32).shape[0]  # NOTE: should match the # of columns in the raw data

    elif probe_path.suffix == '.json':
        with open(probe_path, 'r') as f:
            probe = json.load(f)
        for k in list(probe.keys()):
            # Convert lists back to arrays
            v = probe[k]
            if isinstance(v, list):
                dtype = np.int32 if k == 'chanMap' else np.float32
                probe[k] = np.array(v, dtype=dtype)

    for n in required_keys:
        assert n in probe.keys()

    return probe

  
def save_probe(probe_dict, filepath):
    """Save a probe dictionary to a .json text file.

    Parameters
    ----------
    probe_dict : dict
        A dictionary containing probe information in the format expected by
        Kilosort4, with keys 'chanMap', 'xc', 'yc', and 'kcoords'.
    filepath : str or pathlib.Path
        Location where .json file should be stored.

    Raises
    ------
    RuntimeWarning
        If filepath does not end in '.json'
    
    """

    if Path(filepath).suffix != '.json':
        raise RuntimeWarning(
            'Saving json probe to a file whose suffix is not .json. '
            'kilosort.io.load_probe will not recognize this file.' 
        )

    d = probe_dict.copy()
    # Convert arrays to lists, since arrays aren't json-able
    for k in list(d.keys()):
        v = d[k]
        if isinstance(v, np.ndarray):
            d[k] = v.tolist()
    
    with open(filepath, 'w') as f:
        f.write(json.dumps(d))


def save_to_phy(st, clu, tF, Wall, probe, ops, imin, results_dir=None, data_dtype=None):

    if results_dir is None:
        results_dir = ops['data_dir'].joinpath('kilosort4')
    results_dir.mkdir(exist_ok=True)

    # probe properties
    chan_map = probe['chanMap']
    channel_positions = np.stack((probe['xc'], probe['yc']), axis=-1)
    np.save((results_dir / 'channel_map.npy'), chan_map)
    np.save((results_dir / 'channel_positions.npy'), channel_positions)

    # whitening matrix ** saving real whitening matrix doesn't work with phy currently
    whitening_mat = ops['Wrot'].cpu().numpy()
    np.save((results_dir / 'whitening_mat_dat.npy'), whitening_mat)
    whitening_mat = 0.005 * np.eye(len(chan_map), dtype='float32')
    whitening_mat_inv = np.linalg.inv(whitening_mat + 1e-5 * np.eye(whitening_mat.shape[0]))
    np.save((results_dir / 'whitening_mat.npy'), whitening_mat)
    np.save((results_dir / 'whitening_mat_inv.npy'), whitening_mat_inv)

    # spike properties
    spike_times = st[:,0] + imin  # shift by minimum sample index
    spike_clusters = clu
    amplitudes = ((tF**2).sum(axis=(-2,-1))**0.5).cpu().numpy()
    np.save((results_dir / 'spike_times.npy'), spike_times)
    np.save((results_dir / 'spike_templates.npy'), spike_clusters)
    np.save((results_dir / 'spike_clusters.npy'), spike_clusters)
    np.save((results_dir / 'amplitudes.npy'), amplitudes)

    # template properties
    similar_templates = CCG.similarity(Wall, ops['wPCA'].contiguous(), nt=ops['nt'])
    n_temp = Wall.shape[0]
    template_amplitudes = ((Wall**2).sum(axis=(-2,-1))**0.5).cpu().numpy()
    templates = (Wall.unsqueeze(-1).cpu() * ops['wPCA'].cpu()).sum(axis=-2).numpy()
    templates = templates.transpose(0,2,1)
    templates_ind = np.tile(np.arange(Wall.shape[1])[np.newaxis, :], (templates.shape[0],1))
    np.save((results_dir / 'similar_templates.npy'), similar_templates)
    np.save((results_dir / 'templates.npy'), templates)
    np.save((results_dir / 'templates_ind.npy'), templates_ind)
    
    # contamination ratio
    is_ref, est_contam_rate = CCG.refract(clu, spike_times / ops['fs'])

    # write properties to *.tsv
    stypes = ['ContamPct', 'Amplitude', 'KSLabel']
    ks_labels = [['mua', 'good'][int(r)] for r in is_ref]
    props = [est_contam_rate*100, template_amplitudes, ks_labels]
    for stype, prop in zip(stypes, props):
        with open((results_dir / f'cluster_{stype}.tsv'), 'w') as f:
            f.write(f'cluster_id\t{stype}\n')
            for i,p in enumerate(prop):
                if stype != 'KSLabel':
                    f.write(f'{i}\t{p:.1f}\n')
                else:
                    f.write(f'{i}\t{p}\n')
        if stype == 'KSLabel':
            shutil.copyfile((results_dir / f'cluster_{stype}.tsv'), 
                            (results_dir / f'cluster_group.tsv'))

    # params.py
    dtype = "'int16'" if data_dtype is None else f"'{data_dtype}'"
    params = {'dat_path': f"'{Path(ops['settings']['filename']).as_posix()}'",
            'n_channels_dat': len(chan_map),
            'dtype': dtype,
            'offset': 0,
            'sample_rate': ops['settings']['fs'],
            'hp_filtered': False }
    with open((results_dir / 'params.py'), 'w') as f: 
        for key in params.keys():
            f.write(f'{key} = {params[key]}\n')

    return results_dir, similar_templates, is_ref, est_contam_rate


def save_ops(ops, results_dir=None):
    """Save intermediate `ops` dictionary to `results_dir/ops.npy`."""

    if results_dir is None:
        results_dir = Path(ops['data_dir']) / 'kilosort4'
    else:
        results_dir = Path(results_dir)
    results_dir.mkdir(exist_ok=True)

    ops = ops.copy()
    # Convert paths to strings before saving, otherwise ops can only be loaded
    # on the system that originally ran the code (causes problems for tests).
    ops['settings']['results_dir'] = str(results_dir)
    # TODO: why do these get saved twice?
    ops['filename'] = str(ops['filename'])
    ops['data_dir'] = str(ops['data_dir'])
    ops['settings']['filename'] = str(ops['settings']['filename'])
    ops['settings']['data_dir'] = str(ops['settings']['data_dir'])

    # Convert pytorch tensors to numpy arrays before saving, otherwise loading
    # ops on a different system may not work (if saved from GPU, but loaded
    # on a system with only CPU).
    ops['is_tensor'] = []
    for k, v in ops.items():
        if isinstance(v, torch.Tensor):
            ops[k] = v.cpu().numpy()
            ops['is_tensor'].append(k)
    ops['preprocessing'] = {k: v.cpu().numpy()
                            for k, v in ops['preprocessing'].items()}

    np.save(results_dir / 'ops.npy', np.array(ops))


def load_ops(ops_path, device=torch.device('cuda')):
    """Load a saved `ops` dictionary and convert some arrays to tensors."""

    ops = np.load(ops_path, allow_pickle=True).item()
    for k, v in ops.items():
        if k in ops['is_tensor']:
            ops[k] = torch.from_numpy(v).to(device)
    # TODO: Why do we have one copy of this saved as numpy, one as tensor,
    #       at different levels?
    ops['preprocessing'] = {k: torch.from_numpy(v).to(device)
                            for k,v in ops['preprocessing'].items()}

    return ops


class BinaryRWFile:

    supported_dtypes = ['uint16', 'int16', 'int32', 'float32']

    def __init__(self, filename: str, n_chan_bin: int, fs: int = 30000, 
                 NT: int = 60000, nt: int = 61, nt0min: int = 20,
                 device: torch.device = torch.device('cpu'), write: bool = False,
                 dtype: str = None, tmin: float = 0.0, tmax: float = np.inf,
                 file_object=None):
        """
        Creates/Opens a BinaryFile for reading and/or writing data that acts like numpy array

        * always assume int16 files *

        adapted from https://github.com/MouseLand/suite2p/blob/main/suite2p/io/binary.py
        
        Parameters
        ----------
        filename : str
            The filename of the file to read from or write to
        n_chan_bin : int
            number of channels
        file_object : array-like file object; optional.
            Must have 'shape' and 'dtype' attributes and support array-like
            indexing (e.g. [:100,:], [5, 7:10], etc). For example, a numpy
            array or memmap.

        """
        self.fs = fs
        self.n_chan_bin = n_chan_bin
        self.filename = filename
        self.NT = NT 
        self.nt = nt 
        self.nt0min = nt0min
        self.device = device
        self.uint_set_warning = True
        self.writable = write

        if file_object is not None:
            dtype = file_object.dtype
        if dtype is None:
            dtype = 'int16'
            print("Interpreting binary file as default dtype='int16'. If data was "
                    "saved in a different format, specify `data_dtype`.")
        self.dtype = dtype

        if str(self.dtype) not in self.supported_dtypes:
            message = f"""
                {self.dtype} is not supported and may result in unexpected
                behavior or errors. Supported types are:\n
                {self.supported_dtypes}
                """
            warnings.warn(message, RuntimeWarning)

        # Must come after dtype since dtype is necessary for nbytesread
        if file_object is None:
            total_samples = int(self.nbytes // self.nbytesread)
        else:
            n, c = file_object.shape
            assert c == n_chan_bin
            total_samples = n

        self.imin = max(int(tmin*fs), 0)
        self.imax = total_samples if tmax==np.inf else min(int(tmax*fs), total_samples)

        self.n_batches = int(np.ceil(self.n_samples / self.NT))

        mode = 'w+' if write else 'r'
        # Must use total samples for file shape, otherwise the end of the data
        # gets cut off if tmin,tmax are set.
        if file_object is not None:
            # For an already-loaded array-like file object,
            # such as a NumPy memmap
            self.file = file_object
        else:
            self.file = np.memmap(self.filename, mode=mode, dtype=self.dtype,
                                  shape=(total_samples, self.n_chan_bin))


    @property
    def nbytesread(self):
        """number of bytes per sample (FIXED for given file)"""
        n_bytes = np.dtype(self.dtype).itemsize
        return np.int64(n_bytes * self.n_chan_bin)

    @property
    def nbytes(self):
        """total number of bytes in the file."""
        return os.path.getsize(self.filename)
        
    @property
    def n_samples(self) -> int:
        """total number of samples in the file."""
        return self.imax - self.imin

    @property
    def shape(self) -> Tuple[int, int]:
        """
        The dimensions of the data in the file
        Returns
        -------
        n_samples: int
            number of samples
        n_chan_bin: int
            number of channels
        """
        return self.n_samples, self.n_chan_bin

    @property
    def size(self) -> int:
        """
        Returns the total number of data points

        Returns
        -------
        size: int
        """
        return np.prod(np.array(self.shape).astype(np.int64))

    def close(self) -> None:
        """
        Closes the file.
        """
        del(self.file)
        self.file = None
        
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __setitem__(self, *items):
        if not self.writable:
            raise ValueError('Binary file was loaded as read-only.')

        idx, data = items
        # Shift indices by minimum sample index
        sample_indices = self._get_shifted_indices(idx)
        # Shift data to pos-only
        if self.dtype == 'uint16':
            data = data + 2**15
            if self.uint_set_warning:
                # Inform user of shift to hopefully avoid confusion, but only
                # do this once per bfile.
                print("NOTE: When setting new values for uint16 data, 2**15 will "
                      "be added to the given values before writing to file.")
                self.uint_set_warning = False
        # Convert back from float to file dtype
        data = data.astype(self.dtype)
        self.file[sample_indices] = data
        
    def __getitem__(self, *items):
        if self.file is None:
            raise ValueError('Binary file has been closed, data not accessible.')

        idx, *crop = items
        # Shift indices by minimum sample index.
        sample_indices = self._get_shifted_indices(idx)
        samples = self.file[sample_indices]
        # Shift data to +/- 2**15
        if self.dtype == 'uint16':
            samples = samples - 2**15
            samples = samples.astype('int16')

        return samples
    
    def _get_shifted_indices(self, idx):
        if not isinstance(idx, tuple): idx = tuple([idx])
        new_idx = []

        i = idx[0]
        if isinstance(i, slice):
            # Time dimension
            start = self.imin if i.start is None else i.start + self.imin
            stop = self.imax if i.stop is None else min(i.stop + self.imin, self.imax)
            new_idx.append(slice(start, stop, i.step))
        else:
            new_idx.append(i)

        if len(idx) == 2:
            # Channel dimension, should be no others after this.
            # No adjustments needed.
            new_idx.append(idx[1])

        return tuple(new_idx)

    def padded_batch_to_torch(self, ibatch, return_inds=False):
        """ read batches from file """
        if self.file is None:
            raise ValueError('Binary file has been closed, data not accessible.')

        if ibatch==0:
            bstart = self.imin
            bend = self.imin + self.NT + self.nt
        else:
            bstart = self.imin + (ibatch * self.NT) - self.nt
            bend = min(self.imax, bstart + self.NT + 2*self.nt)
        data = self.file[bstart : bend]
        data = data.T
        # Shift data to +/- 2**15
        if self.dtype == 'uint16':
            data = data - 2**15
            data = data.astype('int16')

        nsamp = data.shape[-1]
        X = torch.zeros((self.n_chan_bin, self.NT + 2*self.nt), device=self.device)
        # fix the data at the edges for the first and last batch
        if ibatch == 0:
            X[:, self.nt : self.nt+nsamp] = torch.from_numpy(data).to(self.device).float()
            X[:, :self.nt] = X[:, self.nt : self.nt+1]
            bstart = self.imin - self.nt
        elif ibatch == self.n_batches-1:
            X[:, :nsamp] = torch.from_numpy(data).to(self.device).float()
            X[:, nsamp:] = X[:, nsamp-1:nsamp]
            bend += self.nt
        else:
            X[:] = torch.from_numpy(data).to(self.device).float()
        inds = [bstart, bend]
        if return_inds:
            return X, inds
        else:
            return X


class BinaryFiltered(BinaryRWFile):
    def __init__(self, filename: str, n_chan_bin: int, fs: int = 30000, 
                 NT: int = 60000, nt: int = 61, nt0min: int = 20,
                 chan_map: np.ndarray = None, hp_filter: torch.Tensor = None,
                 whiten_mat: torch.Tensor = None, dshift: torch.Tensor = None,
                 device: torch.device = torch.device('cuda'), do_CAR: bool = True,
                 invert_sign: bool = False, dtype=None, tmin: float = 0.0,
                 tmax: float = np.inf, file_object=None):

        super().__init__(filename, n_chan_bin, fs, NT, nt, nt0min, device,
                         dtype=dtype, tmin=tmin, tmax=tmax, file_object=file_object) 
        self.chan_map = chan_map
        self.whiten_mat = whiten_mat
        self.hp_filter = hp_filter
        self.dshift = dshift
        self.do_CAR = do_CAR
        self.invert_sign=invert_sign

    def filter(self, X, ops=None, ibatch=None):
        # pick only the channels specified in the chanMap
        if self.chan_map is not None:
            X = X[self.chan_map]

        if self.invert_sign:
            X = X * -1

        X = X - X.mean(1).unsqueeze(1)
        if self.do_CAR:
            # remove the mean of each channel, and the median across channels
            X = X - torch.median(X, 0)[0]
    
        # high-pass filtering in the Fourier domain (much faster than filtfilt etc)
        if self.hp_filter is not None:
            fwav = fft_highpass(self.hp_filter, NT=X.shape[1])
            X = torch.real(ifft(fft(X) * torch.conj(fwav)))
            X = fftshift(X, dim = -1)

        # whitening, with optional drift correction
        if self.whiten_mat is not None:
            if self.dshift is not None and ops is not None and ibatch is not None:
                M = get_drift_matrix(ops, self.dshift[ibatch], device=self.device)
                #print(M.dtype, X.dtype, self.whiten_mat.dtype)
                X = (M @ self.whiten_mat) @ X
            else:
                X = self.whiten_mat @ X
        return X

    def __getitem__(self, *items):
        samples = super().__getitem__(*items)
        X = torch.from_numpy(samples.T).to(self.device).float()
        return self.filter(X)
        
    def padded_batch_to_torch(self, ibatch, ops=None, return_inds=False):
        if return_inds:
            X, inds = super().padded_batch_to_torch(ibatch, return_inds=return_inds)
            return self.filter(X, ops, ibatch), inds
        else:
            X = super().padded_batch_to_torch(ibatch)
            return self.filter(X, ops, ibatch)
