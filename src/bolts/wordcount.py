from streamparse import Bolt, Stream
from ctapipe.image.cleaning import tailcuts_clean
from ctapipe.io import CameraGeometry
from ctapipe.image.hillas import hillas_parameters, \
                                 HillasParameterizationError, \
                                 MomentParameters

from ctapipe.reco.FitGammaHillas import FitGammaHillas
import numpy as np

from astropy import units as u
import astropy
import gzip
import pickle
import os.path
import time

from functools import lru_cache

WORKING_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def deserialize_hillas_moment(moments):
    r = []
    for dct in moments:
        if '__unit__' in dct:
            r.append(dct['__value__']*astropy.units.Unit(dct['__unit__']))
        else:
            r.append(dct['__value__'])
    return MomentParameters._make(r)


def deserialize_hillas_dict(dct):
    hillas_dict = {}
    for tel_id, moment in dct.items():
        hillas_dict[int(tel_id)] = deserialize_hillas_moment(moment)
    return hillas_dict


def serialize_hillas_moment(moments):
    r = []
    for obj in moments:
        if isinstance(obj, astropy.units.Quantity):
            r.append({'__value__': obj.value, '__unit__': obj.unit.name})
        else:
            r.append({'__value__': obj})
    return r


def serialize_dict_with_units(dct):
    d = {}
    for k, v in dct.items():
        if isinstance(v, float) and np.isnan(v):
            d[k] = 'NaN'
        elif isinstance(v, astropy.units.Quantity):
            d[k] = {'__value__': v.value, '__unit__': v.unit.name}
        elif isinstance(v, dict):
            d[k] = serialize_dict_with_units(v)
        else:
            d[k] = v
    return d


def load_instrument():
    p = os.path.join(WORKING_DIR, 'bundled_files', 'instrument.pickle.gz')
    with gzip.open(p, 'rb') as f:
        return pickle.load(f)


class PerfBolt(Bolt):
    outputs = []
    counter = 0
    start_time = time.time()
    sample = 500

    def process(self, tup):
        self.counter += 1
        if self.counter % self.sample == 0:
            self.counter = 0
            current_time = time.time()
            elapsed = current_time - self.start_time

            events_per_sec = self.sample/elapsed
            self.logger.info('recieving  {} event per second'.format(events_per_sec))


class HillasErrorBolt(Bolt):
    outputs = []
    counter = 0

    def process(self, tup):
        self.counter += 1
        self.logger.info('recieved error event number {}'.format(self.counter))


class RecoErrorBolt(Bolt):
    outputs = []
    counter = 0

    def process(self, tup):
        self.counter += 1
        self.logger.info('recieved reco error event number {}'.format(self.counter))


class RecoBolt(Bolt):

    outputs = [Stream(fields=['reconstruction_result'], name='default'),
               Stream(fields=['errors'], name='errors')]

    def initialize(self, conf, ctx):
        self.instrument = load_instrument()
        self.fitter = FitGammaHillas()

    def process(self, tup):
        self.logger.info('recieved tuple')
        hillas_dict = deserialize_hillas_dict(tup.values.hillas)

        r = self.reco(hillas_dict).as_dict()
        if r:
            self.logger.info('emitting reco results')
            self.emit([serialize_dict_with_units(r)])
        else:
            self.logger.warn('event not reconstructed')
            self.emit([1], stream='errors')

    def reco(self, hillas_dict):
        tel_phi = {tel_id: 0*u.deg for tel_id in hillas_dict.keys()}
        tel_theta = {tel_id: 20*u.deg for tel_id in hillas_dict.keys()}
        try:
            self.logger.info('Startign reco')
            fit_result = self.fitter.predict(
                                    hillas_dict,
                                    self.instrument,
                                    tel_phi,
                                    tel_theta)

            self.logger.info('Finished reco')
        except:
            return None
        return fit_result


class HillasBolt(Bolt):

    outputs = [Stream(fields=['hillas'], name='default'),
               Stream(fields=['errors'], name='errors')]

    def initialize(self, conf, ctx):
        self.total_events = 0
        self.instrument = load_instrument()

    def process(self, tup):
        event = tup.values[0]
        self.total_events += 1

        if self.total_events % 25 == 0:
            self.logger.info("counted [{:,}] events [pid={}]".format(self.total_events,
                                                                     self.pid))
        hillas_dict = self.hillas(event)
        if hillas_dict:
            self.logger.info('emitting hillas data')
            self.emit([hillas_dict])
        else:
            self.emit([event['event_id']], stream='errors')

    @lru_cache(maxsize=128)
    def get_cam_geom(self, tel_id):
        pix_x = self.instrument.pixel_pos[int(tel_id)][0]
        pix_y = self.instrument.pixel_pos[int(tel_id)][1]
        foc = self.instrument.optical_foclen[int(tel_id)]
        cam_geom = CameraGeometry.guess(pix_x, pix_y, foc)
        return cam_geom

    def hillas(self, event):
        hillas_dict = {}

        for tel_id in event['data']:
            pmt_signal = np.array(event['data'][tel_id]['adc_sums'])
            cam_geom = self.get_cam_geom(tel_id)
            # self.logger.info('calling tailcuts')
            mask = tailcuts_clean(cam_geom, pmt_signal, 1,
                                  picture_thresh=10., boundary_thresh=5.)
            pmt_signal[mask == 0] = 0

            try:
                moments = hillas_parameters(cam_geom.pix_x,
                                            cam_geom.pix_y,
                                            pmt_signal)

                hillas_dict[tel_id] = serialize_hillas_moment(moments)
            except HillasParameterizationError as e:
                self.logger.warn('Could not calculate hillas parameter')
                return None

        return hillas_dict
