"""Scan and ScanSet classes."""
from __future__ import (absolute_import, unicode_literals, division,
                        print_function)

from .io import read_data, root_name, DEBUG_MODE
import glob
from .read_config import read_config, get_config_file, sample_config_file
import os
import numpy as np
from astropy import wcs
from astropy.table import Table, vstack, Column
import astropy.io.fits as fits
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

from .fit import baseline_rough, baseline_als, linear_fun
from .interactive_filter import select_data
import re
import sys
import warnings
import logging
import traceback


def _rolling_window(a, window):
    """A smart rolling window.

    Found at http://www.rigtorp.se/2011/01/01/rolling-statistics-numpy.html
    """
    try:
        shape = a.shape[:-1] + (a.shape[-1] - window + 1, window)
        strides = a.strides + (a.strides[-1],)
        return np.lib.stride_tricks.as_strided(a, shape=shape, strides=strides)
    except Exception as e:
        print(a.shape)
        print(a)
        print(window)
        traceback.print_exc()
        raise


chan_re = re.compile(r'^Ch[0-9]+$')


def list_scans(datadir, dirlist):
    """List all scans contained in the directory listed in config."""
    scan_list = []

    for d in dirlist:
        for f in glob.glob(os.path.join(datadir, d, '*.fits')):
            scan_list.append(f)
    return scan_list


class Scan(Table):
    """Class containing a single scan."""

    def __init__(self, data=None, config_file=None, norefilt=False,
                 interactive=False, nosave=False, verbose=True,
                 freqsplat=None, **kwargs):
        """Initialize a Scan object.

        Freqsplat is a string, freqmin:freqmax, and gives the limiting
        frequencies of the interval to splat in a single channel.
        """
        if config_file is None:
            config_file = get_config_file()

        if isinstance(data, Table):
            Table.__init__(self, data, **kwargs)
        elif data is None:
            Table.__init__(self, **kwargs)
            self.meta['config_file'] = config_file
            self.meta.update(read_config(self.meta['config_file']))
        else:  # if data is a filename
            if os.path.exists(root_name(data) + '.hdf5'):
                data = root_name(data) + '.hdf5'
            if verbose:
                logging.info('Loading file {}'.format(data))
            table = read_data(data)
            Table.__init__(self, table, masked=True, **kwargs)
            self.meta['filename'] = os.path.abspath(data)
            self.meta['config_file'] = config_file

            self.meta.update(read_config(self.meta['config_file']))

            self.check_order()

            self.clean_and_splat(freqsplat=freqsplat)

            if interactive:
                self.interactive_filter()

            if ('backsub' not in self.meta.keys() or
                    not self.meta['backsub']) \
                    and not norefilt:
                logging.info('Subtracting the baseline')
                self.baseline_subtract()

            if not nosave:
                self.save()

    def interpret_frequency_range(self, freqsplat, bandwidth, nbin):
        """Interpret the frequency range specified in freqsplat."""
        try:
            freqmin, freqmax = \
                [float(f) for f in freqsplat.split(':')]
        except:
            freqsplat = ":"

        if freqsplat == ":" or freqsplat == "all" or freqsplat is None:
            freqmin = 0
            freqmax = bandwidth

        binmin = int(nbin * freqmin / bandwidth)
        binmax = int(nbin * freqmax / bandwidth)

        return freqmin, freqmax, binmin, binmax

    def make_single_channel(self, freqsplat, masks=None):
        """Transform a spectrum into a single-channel count rate."""
        for ic, ch in enumerate(self.chan_columns()):
            if len(self[ch].shape) == 1:
                continue

            _, nbin = self[ch].shape

            freqmin, freqmax, binmin, binmax = \
                self.interpret_frequency_range(freqsplat,
                                               self[ch].meta['bandwidth'],
                                               nbin)

            if masks is not None:
                self[ch][:, np.logical_not(masks[ch])] = 0

            self[ch + 'TEMP'] = \
                Column(np.sum(self[ch][:, binmin:binmax], axis=1))

            self[ch + 'TEMP'].meta.update(self[ch].meta)
            self.remove_column(ch)
            self[ch + 'TEMP'].name = ch
            self[ch].meta['bandwidth'] = freqmax - freqmin

    def chan_columns(self):
        """List columns containing samples."""
        return np.array([i for i in self.columns
                         if chan_re.match(i)])

    def clean_and_splat(self, good_mask=None, freqsplat=None, debug=True,
                        save_spectrum=False):
        """Clean from RFI.

        Very rough now, it will become complicated eventually.

        Parameters
        ----------
        good_mask : boolean array
            this mask specifies intervals that should never be discarded as
            RFI, for example because they contain spectral lines
        freqsplat : str
            Specification of frequency interval to merge into a single channel

        Returns
        -------
        masks : dictionary of boolean arrays
            this dictionary contains, for each detector/polarization, True
            values for good spectral channels, and False for bad channels.

        Other parameters
        ----------------
        save_spectrum : bool, default False
            Save the spectrum into a 'ChX_spec' column
        debug : bool, default True
            Save images with quicklook information on single scans
        """
        if self.meta['filtering_factor'] > 0.5:
            warnings.warn("Don't use filtering factors > 0.5. Skipping.")
            return

        chans = self.chan_columns()
        for ic, ch in enumerate(chans):
            if len(self[ch].shape) == 1:
                break
            _, nbin = self[ch].shape

            lc = np.sum(self[ch], axis=1)
            lc = baseline_als(self['time'], lc)
            lcbins = np.arange(len(lc))
            total_spec = np.sum(self[ch], axis=0) / len(self[ch])
            spectral_var = \
                np.sqrt(np.sum((self[ch] - total_spec) ** 2 / total_spec ** 2,
                        axis=0))

            allbins = np.arange(len(total_spec))

            freqmask = np.ones(len(total_spec), dtype=bool)

            freqmin, freqmax, binmin, binmax = \
                self.interpret_frequency_range(freqsplat,
                                               self[ch].meta['bandwidth'],
                                               nbin)
            freqmask[0:binmin] = False
            freqmask[binmax:] = False

            if debug:
                fig = plt.figure("{}_{}".format(self.meta['filename'], ic))
                gs = GridSpec(3, 2, hspace=0, height_ratios=(1.5, 3, 1.5),
                              width_ratios=(3, 1.5))
                ax1 = plt.subplot(gs[0, 0])
                ax2 = plt.subplot(gs[1, 0], sharex=ax1)
                ax3 = plt.subplot(gs[1, 1], sharey=ax2)
                ax4 = plt.subplot(gs[2, 0], sharex=ax1)
                ax1.plot(total_spec, label="Unfiltered")
                ax4.plot(spectral_var, label="Spectral rms")

            if good_mask is not None:
                total_spec[good_mask] = 0

            varimg = np.sqrt((self[ch] - total_spec) ** 2 / total_spec ** 2)
            mean_varimg = np.mean(varimg[:, freqmask])
            std_varimg = np.std(varimg[:, freqmask])

            ref_std = np.min(
                np.std(_rolling_window(spectral_var[freqmask],
                       np.max([nbin // 20, 20])), 1))

            np.std(spectral_var[freqmask])

            _, baseline = baseline_als(np.arange(len(spectral_var)),
                                       spectral_var, return_baseline=True,
                                       lam=1000, p=0.001)
            threshold = baseline + 5 * ref_std

            mask = spectral_var < threshold

            wholemask = freqmask & mask
            lc_corr = np.sum(self[ch][:, wholemask], axis=1)
            lc_corr = baseline_als(self['time'], lc_corr)

            if debug:
                ax1.plot(total_spec, label="Whitelist applied")
                ax1.axvline(binmin)
                ax1.axvline(binmax)
                ax1.plot(allbins[mask], total_spec[mask],
                         label="Final mask")
                ax1.legend()

                ax2.imshow(varimg, origin="lower", aspect='auto',
                           cmap=plt.get_cmap("magma"),
                           vmin=mean_varimg - 5 * std_varimg,
                           vmax=mean_varimg + 5 * std_varimg)

                ax2.axvline(binmin)
                ax2.axvline(binmax)

                ax3.plot(lc, lcbins)
                ax3.plot(lc_corr, lcbins)
                ax3.set_xlim([np.min(lc), max(lc)])
                ax3.axvline(binmin)
                ax3.axvline(binmax)
                ax4.plot(allbins[mask], spectral_var[mask])
                ax4.plot(allbins, baseline)

                plt.savefig(
                    "{}_{}.pdf".format(
                        (self.meta['filename'].replace('.fits', '')
                         ).replace('.hdf5', ''), ic))
                plt.close(fig)

            self[ch + 'TEMP'] = Column(lc_corr)

            self[ch + 'TEMP'].meta.update(self[ch].meta)
            if save_spectrum:
                self[ch].name = ch + "_spec"
            else:
                self.remove_column(ch)
            self[ch + 'TEMP'].name = ch
            self[ch].meta['bandwidth'] = freqmax - freqmin

    def baseline_subtract(self, kind='als'):
        """Subtract the baseline."""
        if kind == 'als':

            for col in self.chan_columns():
                self[col] = baseline_als(self['time'], self[col])
        elif kind == 'rough':
            for col in self.chan_columns():
                self[col] = baseline_rough(self['time'], self[col])

        self.meta['backsub'] = True

    def zap_birdies(self):
        """Zap bad intervals."""
        pass

    def __repr__(self):
        """Give the print() function something to print."""
        reprstring = \
            '\n\n----Scan from file {0} ----\n'.format(self.meta['filename'])
        reprstring += repr(Table(self))
        return reprstring

    def write(self, fname, **kwargs):
        """Set default path and call Table.write."""
        logging.info('Saving to {}'.format(fname))
        t = Table(self)
        t.write(fname, path='scan', **kwargs)

    def check_order(self):
        """Check that times in a scan are monotonically increasing."""
        assert np.all(self['time'] == np.sort(self['time'])), \
            'The order of times in the table is wrong'

    def interactive_filter(self, save=True):
        """Run the interactive filter."""
        for ch in self.chan_columns():
            # Temporary, waiting for AstroPy's metadata handling improvements
            feed = self[ch + '_feed'][0]

            selection = self['ra'][:, feed]

            ravar = np.abs(selection[-1] -
                           selection[0])

            selection = self['dec'][:, feed]
            decvar = np.abs(selection[-1] -
                            selection[0])

            # Choose if plotting by R.A. or Dec.
            if ravar > decvar:
                dim = 'ra'
            else:
                dim = 'dec'

            # ------- CALL INTERACTIVE FITTER ---------
            info = select_data(self[dim][:, feed], self[ch],
                               xlabel=dim)

            # -----------------------------------------

            # Treat zapped intervals
            xs = info['Ch']['zap'].xs
            good = np.ones(len(self[dim]), dtype=bool)
            if len(xs) >= 2:
                intervals = list(zip(xs[:-1:2], xs[1::2]))
                for i in intervals:
                    good[np.logical_and(self[dim][:, feed] >= i[0],
                                        self[dim][:, feed] <= i[1])] = False
            self['{}-filt'.format(ch)] = good

            if len(info['Ch']['fitpars']) > 1:
                self[ch] -= linear_fun(self[dim][:, feed],
                                       *info['Ch']['fitpars'])
            # TODO: make it channel-independent
                self.meta['backsub'] = True

            # TODO: make it channel-independent
            if info['Ch']['FLAG']:
                self.meta['FLAG'] = True
        if save:
            self.save()
        self.meta['ifilt'] = True

    def save(self, fname=None):
        """Call self.write with a default filename, or specify it."""
        if fname is None:
            fname = root_name(self.meta['filename']) + '.hdf5'
        self.write(fname, overwrite=True)


class ScanSet(Table):
    """Class containing a set of scans."""

    def __init__(self, data=None, norefilt=True, config_file=None,
                 freqsplat=None, **kwargs):
        """Initialize a ScanSet object."""
        self.norefilt = norefilt
        if isinstance(data, Table):
            Table.__init__(self, data, **kwargs)
            if config_file is not None:
                config = read_config(config_file)
                self.meta.update(config)

            self.create_wcs()
        else:  # data is a config file
            config_file = data
            config = read_config(config_file)
            scan_list = \
                self.list_scans(config['datadir'],
                                config['list_of_directories'])

            scan_list.sort()
            # nscans = len(scan_list)

            tables = []

            for i_s, s in self.load_scans(scan_list,
                                          freqsplat=freqsplat, **kwargs):

                if 'FLAG' in s.meta.keys() and s.meta['FLAG']:
                    continue
                s['Scan_id'] = i_s + np.zeros(len(s['time']), dtype=np.long)

                tables.append(s)

            scan_table = Table(vstack(tables))

            Table.__init__(self, scan_table)
            self.meta['scan_list'] = scan_list
            self.meta.update(config)
            self.meta['config_file'] = get_config_file()

            self.meta['scan_list'] = np.array(self.meta['scan_list'],
                                              dtype='S')
            self.analyze_coordinates(altaz=False)
            self.analyze_coordinates(altaz=True)

            self.convert_coordinates()

        self.chan_columns = np.array([i for i in self.columns
                                      if chan_re.match(i)])
        self.current = None

    def analyze_coordinates(self, altaz=False):
        """Save statistical information on coordinates."""
        if altaz:
            hor, ver = 'az', 'el'
        else:
            hor, ver = 'ra', 'dec'

        allhor = self[hor]
        allver = self[ver]

        self.meta['mean_' + hor] = np.mean(allhor)
        self.meta['mean_' + ver] = np.mean(allver)
        self.meta['min_' + hor] = np.min(allhor)
        self.meta['min_' + ver] = np.min(allver)
        self.meta['max_' + hor] = np.max(allhor)
        self.meta['max_' + ver] = np.max(allver)

    def list_scans(self, datadir, dirlist):
        """List all scans contained in the directory listed in config."""
        return list_scans(datadir, dirlist)

    def load_scans(self, scan_list, freqsplat=None, **kwargs):
        """Load the scans in the list one by ones."""
        for i, f in enumerate(scan_list):
            try:
                s = Scan(f, norefilt=self.norefilt, freqsplat=freqsplat,
                         **kwargs)
                yield i, s
            except Exception as e:
                traceback.print_exc()
                warnings.warn("Error while processing {}: {}".format(f,
                                                                     str(e)))

    def get_coordinates(self, altaz=False):
        """Give the coordinates as pairs of RA, DEC."""
        if altaz:
            return np.array(np.dstack([self['az'],
                                       self['el']]))
        else:
            return np.array(np.dstack([self['ra'],
                                       self['dec']]))

    def create_wcs(self, altaz=False):
        """Create a wcs object from the pointing information."""
        if altaz:
            hor, ver = 'az', 'el'
        else:
            hor, ver = 'ra', 'dec'
        npix = np.array(self.meta['npix'])
        self.wcs = wcs.WCS(naxis=2)

        self.wcs.wcs.crpix = npix / 2
        delta_hor = self.meta['max_' + hor] - self.meta['min_' + hor]
        delta_ver = self.meta['max_' + ver] - self.meta['min_' + ver]

        if not hasattr(self.meta, 'reference_' + hor):
            self.meta['reference_' + hor] = self.meta['mean_' + hor]
        if not hasattr(self.meta, 'reference_' + ver):
            self.meta['reference_' + ver] = self.meta['mean_' + ver]

        # TODO: check consistency of units
        # Here I'm assuming all angles are radians
        crval = np.array([self.meta['reference_' + hor],
                          self.meta['reference_' + ver]])
        self.wcs.wcs.crval = np.degrees(crval)

        cdelt = np.array([-delta_hor / npix[0],
                          delta_ver / npix[1]])
        self.wcs.wcs.cdelt = np.degrees(cdelt)

        self.wcs.wcs.ctype = \
            ["RA---{}".format(self.meta['projection']),
             "DEC--{}".format(self.meta['projection'])]

#    def scrunch_channels(self, feeds=None, polarizations=None,
#                         chan_names=None):
#        """Scrunch channels and reduce their number.
#
#        POLARIZATIONS NOT IMPLEMENTED YET!
#        2-D lists of channels NOT IMPLEMENTED YET!
#
#        feed and polarization filters can be given as:
#
#        None:          all channels are to be summed in one
#        list of chans: channels in this list are summed, the others are
#                       deleted only one channel remains
#        2-d array:     the channels arr[0, :] will go to chan 0, arr[1, :] to
#                       chan 1, and so on.
#
#        At the end of the process, all channels have been eliminated but the
#        ones selected.
#        The axis-1 length of feeds and polarizations MUST be the same, unless
#        one of them is None.
#        """
#        # TODO: Implement polarizations
#        # TODO: Implement 2-d arrays
#
#        allfeeds = np.array([self[ch + '_feed'][0]
#                             for ch in self.chan_columns])
#        if feeds is None:
#            feeds = list(set(allfeeds))
#
#        feed_mask = np.in1d(allfeeds, feeds)

    def convert_coordinates(self, altaz=False):
        """Convert the coordinates from sky to pixel."""
        if altaz:
            hor, ver = 'az', 'el'
        else:
            hor, ver = 'ra', 'dec'
        self.create_wcs(altaz)

        self['x'] = np.zeros_like(self[hor])
        self['y'] = np.zeros_like(self[ver])
        coords = np.degrees(self.get_coordinates())
        for f in range(len(self[hor][0, :])):
            pixcrd = self.wcs.wcs_world2pix(coords[:, f], 0)

            self['x'][:, f] = pixcrd[:, 0]
            self['y'][:, f] = pixcrd[:, 1]

    def calculate_images(self, scrunch=False, no_offsets=False, altaz=False):
        """Obtain image from all scans.

        scrunch:         sum all channels
        no_offsets:      use positions from feed 0 for all feeds.
        """
        images = {}
        # xbins = np.linspace(np.min(self['x']),
        #                     np.max(self['x']),
        #                     self.meta['npix'][0] + 1)
        # ybins = np.linspace(np.min(self['y']),
        #                     np.max(self['y']),
        #                     self.meta['npix'][1] + 1)
        xbins = np.linspace(0,
                            self.meta['npix'][0],
                            self.meta['npix'][0] + 1)
        ybins = np.linspace(0,
                            self.meta['npix'][1],
                            self.meta['npix'][1] + 1)

        total_expo = 0
        total_img = 0
        total_sdev = 0
        for ch in self.chan_columns:
            feeds = self[ch+'_feed']
            allfeeds = list(set(feeds))
            assert len(allfeeds) == 1, 'Feeds are mixed up in channels'
            if no_offsets:
                feed = 0
            else:
                feed = feeds[0]

            if '{}-filt'.format(ch) in self.keys():
                good = self['{}-filt'.format(ch)]
            else:
                good = np.ones(len(self[ch]), dtype=bool)

            expomap, _, _ = np.histogram2d(self['x'][:, feed][good],
                                           self['y'][:, feed][good],
                                           bins=[xbins, ybins])

            img, _, _ = np.histogram2d(self['x'][:, feed][good],
                                       self['y'][:, feed][good],
                                       bins=[xbins, ybins],
                                       weights=self[ch][good])
            img_sq, _, _ = np.histogram2d(self['x'][:, feed][good],
                                          self['y'][:, feed][good],
                                          bins=[xbins, ybins],
                                          weights=self[ch][good] ** 2)

            good = expomap > 0
            mean = img.copy()
            total_img += mean.T
            mean[good] /= expomap[good]
            # For Numpy vs FITS image conventions...
            images[ch] = mean.T
            img_sdev = img_sq
            total_sdev += img_sdev.T
            img_sdev[good] = img_sdev[good] / expomap[good] - mean[good] ** 2

            images['{}-Sdev'.format(ch)] = img_sdev.T
            total_expo += expomap.T

        self.images = images
        if scrunch:
            # Filter the part of the image whose value of exposure is higher
            # than the 10% percentile (avoid underexposed parts)
            good = total_expo > np.percentile(total_expo, 10)
            bad = np.logical_not(good)
            total_img[bad] = 0
            total_sdev[bad] = 0
            total_img[good] /= total_expo[good]
            total_sdev[good] = total_sdev[good] / total_expo[good] - \
                total_img[good] ** 2

            images = {self.chan_columns[0]: total_img,
                      '{}-Sdev'.format(self.chan_columns[0]): total_sdev,
                      '{}-EXPO'.format(self.chan_columns[0]): total_expo}

        return images

    def interactive_display(self, ch=None, recreate=False):
        """Modify original scans from the image display."""
        from .interactive_filter import ImageSelector

        if not hasattr(self, 'images') or recreate:
            self.calculate_images()

        if ch is None:
            chs = self.chan_columns
        else:
            chs = [ch]
        for ch in chs:
            fig = plt.figure('Imageactive Display')
            gs = GridSpec(1, 2, width_ratios=(3, 2))
            ax = fig.add_subplot(gs[0])
            ax2 = fig.add_subplot(gs[1])
            img = self.images[ch]
            ax2.imshow(img, origin='lower',
                       vmin=np.percentile(img, 20), cmap="gnuplot2",
                       interpolation="nearest")

            img = self.images['{}-Sdev'.format(ch)]
            self.current = ch
            ImageSelector(img, ax, fun=self.rerun_scan_analysis)

    def rerun_scan_analysis(self, x, y, key):
        """Rerun the analysis of single scans."""
        logging.debug(x, y, key)
        if key == 'a':
            self.reprocess_scans_through_pixel(x, y)
        elif key == 'h':
            pass
        elif key == 'v':
            pass

    def reprocess_scans_through_pixel(self, x, y):
        """Given a pixel in the image, find all scans passing through it."""
        ch = self.current

        ra_xs, ra_ys, dec_xs, dec_ys, scan_ids, ra_masks, dec_masks, \
            vars_to_filter = \
            self.find_scans_through_pixel(x, y)

        info = select_data(ra_xs, ra_ys, masks=ra_masks,
                           xlabel="RA", title="RA")

        for sname in info.keys():
            self.update_scan(sname, scan_ids[sname], vars_to_filter[sname],
                             info[sname]['zap'],
                             info[sname]['fitpars'], info[sname]['FLAG'])

        info = select_data(dec_xs, dec_ys, masks=dec_masks, xlabel="Dec",
                           title="Dec")

        for sname in info.keys():
            self.update_scan(sname, scan_ids[sname], vars_to_filter[sname],
                             info[sname]['zap'],
                             info[sname]['fitpars'], info[sname]['FLAG'])

        self.interactive_display(ch=ch, recreate=True)

    def find_scans_through_pixel(self, x, y):
        """Find scans passing through a pixel."""
        ra_xs = {}
        ra_ys = {}
        dec_xs = {}
        dec_ys = {}
        scan_ids = {}
        ra_masks = {}
        dec_masks = {}
        vars_to_filter = {}

        ch = self.current
        feed = list(set(self[ch+'_feed']))[0]

        # Select data inside the pixel +- 1

        good_entries = \
            np.logical_and(
                np.abs(self['x'][:, feed] - x) < 1,
                np.abs(self['y'][:, feed] - y) < 1)

        sids = list(set(self['Scan_id'][good_entries]))

        for sid in sids:
            sname = self.meta['scan_list'][sid].decode()
            try:
                s = Scan(sname)
            except:
                continue
            try:
                chan_mask = s['{}-filt'.format(ch)]
            except:
                chan_mask = np.zeros_like(s[ch])

            scan_ids[sname] = sid
            ras = s['ra'][:, feed]
            decs = s['dec'][:, feed]

            z = s[ch]

            ravar = np.max(ras) - np.min(ras)
            decvar = np.max(decs) - np.min(decs)
            if ravar > decvar:
                vars_to_filter[sname] = 'ra'
                ra_xs[sname] = ras
                ra_ys[sname] = z
                ra_masks[sname] = chan_mask
            else:
                vars_to_filter[sname] = 'dec'
                dec_xs[sname] = decs
                dec_ys[sname] = z
                dec_masks[sname] = chan_mask

        return ra_xs, ra_ys, dec_xs, dec_ys, scan_ids, ra_masks, dec_masks, \
            vars_to_filter

    def update_scan(self, sname, sid, dim, zap_info, fit_info, flag_info):
        """Update a scan in the scanset after filtering."""
        ch = self.current
        feed = list(set(self[ch+'_feed']))[0]
        mask = self['Scan_id'] == sid
        try:
            s = Scan(sname)
        except:
            return

        if len(zap_info.xs) > 0:

            xs = zap_info.xs
            good = np.ones(len(s[dim]), dtype=bool)
            if len(xs) >= 2:
                intervals = list(zip(xs[:-1:2], xs[1::2]))
                for i in intervals:
                    good[np.logical_and(s[dim][:, feed] >= i[0],
                                        s[dim][:, feed] <= i[1])] = False
            s['{}-filt'.format(ch)] = good
            self['{}-filt'.format(ch)][mask] = good

        if len(fit_info) > 1:
            s[ch] -= linear_fun(s[dim][:, feed],
                                *fit_info)
        # TODO: make it channel-independent
            s.meta['backsub'] = True
            try:
                self[ch][mask][:] = s[ch]
            except:
                warnings.warn("Something while treating {}".format(sname))

                plt.figure("DEBUG")
                plt.plot(self['ra'][mask], self['dec'][mask])
                plt.show()
                raise

        # TODO: make it channel-independent
        if flag_info:
            s.meta['FLAG'] = True
            self['{}-filt'.format(ch)][mask] = np.zeros(len(s[dim]),
                                                        dtype=bool)

        s.save()

    def write(self, fname, **kwargs):
        """Set default path and call Table.write."""
        t = Table(self)
        t.write(fname, path='scanset', **kwargs)

    def save_ds9_images(self, fname=None, save_sdev=False, scrunch=False,
                        no_offsets=False, altaz=False):
        """Save a ds9-compatible file with one image per extension."""
        if fname is None:
            fname = 'img.fits'
        images = self.calculate_images(scrunch=scrunch, no_offsets=no_offsets,
                                       altaz=altaz)
        self.create_wcs(altaz)

        hdulist = fits.HDUList()

        header = self.wcs.to_header()

        hdu = fits.PrimaryHDU(header=header)
        hdulist.append(hdu)

        keys = list(images.keys())
        keys.sort()
        for ic, ch in enumerate(keys):
            is_sdev = ch.endswith('Sdev')

            if is_sdev and not save_sdev:
                continue

            hdu = fits.ImageHDU(images[ch], header=header, name='IMG' + ch)
            hdulist.append(hdu)

        hdulist.writeto(fname, clobber=True)


def main_imager(args=None):
    """Main function."""
    import argparse

    description = ('Load a series of scans from a config file '
                   'and produce a map.')
    parser = argparse.ArgumentParser(description=description)

    parser.add_argument("--sample-config", action='store_true', default=False,
                        help='Produce sample config file')

    parser.add_argument("-c", "--config", type=str, default=None,
                        help='Config file')

    parser.add_argument("--refilt", default=False,
                        action='store_true',
                        help='Re-run the scan filtering')

    parser.add_argument("--interactive", default=False,
                        action='store_true',
                        help='Open the interactive display')

    parser.add_argument("--splat", type=str, default=None,
                        help=("Spectral scans will be scrunched into a single "
                              "channel containing data in the given frequency "
                              "range, starting from the frequency of the first"
                              " bin. E.g. '0:1000' indicates 'from the first "
                              "bin of the spectrum up to 1000 MHz above'. ':' "
                              "or 'all' for all the channels."))

    args = parser.parse_args(args)

    if args.sample_config:
        sample_config_file()
        sys.exit()

    assert args.config is not None, "Please specify the config file!"

    scanset = ScanSet(args.config, norefilt=not args.refilt,
                      freqsplat=args.splat)

    scanset.write('test.hdf5', overwrite=True)

    scanset = ScanSet(Table.read('test.hdf5', path='scanset'),
                      config_file=args.config,
                      freqsplat=args.splat)

    scanset.calculate_images()

    if args.interactive:
        scanset.interactive_display()
    scanset.save_ds9_images(save_sdev=True)
