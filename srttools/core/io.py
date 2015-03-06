import astropy.io.fits as fits
from astropy.table import Table
import numpy as np


def detect_data_kind(fname):
    '''Placeholder for function that recognizes data format.'''
    return 'Filezilla'


def print_obs_info_filezilla(fname):
    '''Placeholder for function that prints out oberving information.'''
    lchdulist = fits.open(fname)
    section_table_data = lchdulist['SECTION TABLE'].data
    sample_rates = section_table_data['sampleRate']

    print('Sample rate:', set(sample_rates))

    rf_input_data = lchdulist['RF INPUTS'].data
    print('Feeds          :', set(rf_input_data['feed']))
    print('IF             :', set(rf_input_data['ifChain']))
    print('Polarizations  :', set(rf_input_data['polarization']))
    print('Frequency      :', set(rf_input_data['frequency']))
    print('Bandwidth      :', set(rf_input_data['bandWidth']))

    lchdulist.close()
    pass


def read_data_filezilla(fname):
    '''Open a Filezilla FITS file and read all relevant information.'''
    lchdulist = fits.open(fname)

    section_table_data = lchdulist['SECTION TABLE'].data
    chan_ids = section_table_data['id']

    data_table_data = lchdulist['DATA TABLE'].data

    info_to_retrieve = ['time', 'raj2000', 'decj2000', 'az', 'el', 'derot_angle']

    new_table = Table()
    for info in info_to_retrieve:
        new_table[info] = data_table_data[info]

    chans = np.zeros((len(new_table['time']), len(chan_ids)))
    for i in chan_ids:
        chans[:, i] = data_table_data['Ch{}'.format(i)]

    new_table['data'] = chans

    lchdulist.close()
    return new_table


def read_data(fname):
    '''Read the data, whatever the format, and return them'''
    kind = detect_data_kind(fname)
    if kind == 'Filezilla':
        return read_data_filezilla(fname)


class Scan():
    '''Class containing a single scan'''
    def __init__(self, fname):
        self.table = read_data(fname)


def test_open_data_filezilla():
    '''Test that data are read.'''
    import os
    curdir = os.path.abspath(os.path.dirname(__file__))
    datadir = os.path.join(curdir, '..', '..', 'TEST_DATASET')

    fname = os.path.join(datadir, '20140603-103246-scicom-3C157',
                         '20140603-103246-scicom-3C157_003_003.fits')
    print_obs_info_filezilla(fname)
    table = read_data(fname)
    print(table)

