import gobject
import pygst
pygst.require("0.10")
import gst
import time
import re
from openob.logger import LoggerFactory


class RTPTransmitter(object):

    def __init__(self, node_name, link_config, audio_interface):
        """Sets up a new RTP transmitter"""
        self.started = False
        self.caps = 'None'
        self.pipeline = gst.Pipeline("tx")
        self.bus = self.pipeline.get_bus()
        self.bus.connect("message", self.on_message)
        self.link_config = link_config
        self.audio_interface = audio_interface
        self.logger_factory = LoggerFactory()
        self.logger = self.logger_factory.getLogger('node.%s.link.%s.%s' % (node_name, self.link_config.name, self.audio_interface.mode))
        self.logger.info("Starting up RTP transmitter")
        # Audio input
        if self.audio_interface.type == 'auto':
            self.source = gst.element_factory_make('autoaudiosrc')
        elif self.audio_interface.type == 'alsa':
            self.source = gst.element_factory_make('alsasrc')
            self.source.set_property('device', self.audio_interface.device)
        elif self.audio_interface.type == 'jack':
            self.source = gst.element_factory_make("jackaudiosrc")
            if self.audio_interface.jack_auto:
                self.source.set_property('connect', 'auto')
            else:
                self.source.set_property('connect', 'none')
            self.source.set_property('name', self.audio_interface.jack_name)
        # Audio conversion and resampling
        self.audioconvert = gst.element_factory_make("audioconvert")
        self.audioresample = gst.element_factory_make("audioresample")
        self.audioresample.set_property('quality', 9)  # SRC
        self.audiorate = gst.element_factory_make("audiorate")

        # Encoding and payloading
        if self.link_config.encoding == 'opus':
            self.encoder = gst.element_factory_make("opusenc", "encoder")
            self.encoder.set_property('bitrate', int(self.link_config.bitrate) * 1000)
            self.encoder.set_property('frame-size', self.link_config.opus_framesize)
            self.encoder.set_property('complexity', int(self.link_config.opus_complexity))
            self.encoder.set_property('inband-fec', self.link_config.opus_fec)
            self.encoder.set_property('packet-loss-percentage', int(self.link_config.opus_loss_expectation))
            self.encoder.set_property('dtx', self.link_config.opus_dtx)
            self.payloader = gst.element_factory_make("rtpopuspay", "payloader")
        elif self.link_config.encoding == 'pcm':
            # we have no encoder for PCM operation
            self.payloader = gst.element_factory_make("rtpL16pay", "payloader")
        else:
            self.logger.critical("Unknown encoding type %s" % self.link_config.encoding)
        # TODO: Add a tee here, and sort out creating multiple UDP sinks for multipath
        # Now the RTP bits
        # We'll send audio out on this
        self.udpsink_rtpout = gst.element_factory_make(
            "udpsink", "udpsink_rtp")
        self.udpsink_rtpout.set_property('host', self.link_config.receiver_host)
        self.udpsink_rtpout.set_property('port', self.link_config.port)
        if self.link_config.multicast:
            self.udpsink_rtpout.set_property('auto_multicast', True)
        # # And send our control packets out on this
        # self.udpsink_rtcpout = gst.element_factory_make(
        #     "udpsink", "udpsink_rtcp")
        # self.udpsink_rtcpout.set_property('host', self.link_config.receiver_host)
        # self.udpsink_rtcpout.set_property('port', self.link_config.port + 1)
        # # And the receiver will send us RTCP Sender Reports on this
        # self.udpsrc_rtcpin = gst.element_factory_make("udpsrc", "udpsrc_rtcp")
        # self.udpsrc_rtcpin.set_property('port', self.link_config.port + 2)
        # # (but we'll ignore them/operate fine without them because we assume we're stuck behind a firewall)
        # Our RTP manager
        self.rtpbin = gst.element_factory_make("gstrtpbin", "gstrtpbin")

        # Our level monitor
        self.level = gst.element_factory_make("level")
        self.level.set_property('message', True)
        self.level.set_property('interval', 1000000000)

        # Add a capsfilter to allow specification of input sample rate
        self.capsfilter = gst.element_factory_make("capsfilter")

        # Add to the pipeline
        self.pipeline.add(
            self.source, self.capsfilter, self.audioconvert, self.audioresample,
            self.audiorate, self.payloader, self.udpsink_rtpout, self.rtpbin,
            self.level)

        if self.link_config.encoding != 'pcm':
            # Only add the encoder if we're not in PCM mode
            self.pipeline.add(self.encoder)

        # Decide which format to apply to the capsfilter (Jack uses float)
        if self.audio_interface.type == 'jack':
            type = 'audio/x-raw-float'
        else:
            type = 'audio/x-raw-int'

        # if audio_rate has been specified, then add that to the capsfilter
        if self.audio_interface.samplerate != 0:
            self.capsfilter.set_property(
                "caps", gst.Caps('%s, channels=2, rate=%d' % (type, self.audio_interface.samplerate)))
        else:
            self.capsfilter.set_property(
                "caps", gst.Caps('%s, channels=2' % type))

        # Then continue linking the pipeline together
        gst.element_link_many(
            self.source, self.capsfilter, self.level, self.audioresample,
            self.audiorate, self.audioconvert)

        # Now we get to link this up to our encoder/payloader
        if self.link_config.encoding != 'pcm':
            gst.element_link_many(
                self.audioconvert, self.encoder, self.payloader)
        else:
            gst.element_link_many(self.audioconvert, self.payloader)

        # And now the RTP bits
        self.payloader.link_pads('src', self.rtpbin, 'send_rtp_sink_0')
        self.rtpbin.link_pads('send_rtp_src_0', self.udpsink_rtpout, 'sink')
        # self.udpsrc_rtcpin.link_pads('src', self.rtpbin, 'recv_rtcp_sink_0')
        # # RTCP SRs
        # self.rtpbin.link_pads('send_rtcp_src_0', self.udpsink_rtcpout, 'sink')
        # Connect our bus up
        self.bus.add_signal_watch()
        self.bus.connect('message', self.on_message)

    def run(self):
        self.udpsink_rtpout.set_locked_state(gst.STATE_PLAYING)
        self.pipeline.set_state(gst.STATE_PLAYING)
        while self.caps == 'None':
            self.logger.warn("Waiting for audio interface/caps")
            self.logger.debug(self.udpsink_rtpout.get_state())
            self.caps = str(
                self.udpsink_rtpout.get_pad('sink').get_property('caps'))
            # Fix for gstreamer bug in rtpopuspay fixed in GST-plugins-bad
            # 50140388d2b62d32dd9d0c071e3051ebc5b4083b, bug 686547
            if self.link_config.encoding == 'opus':
                self.caps = re.sub(r'(caps=.+ )', '', self.caps)
            time.sleep(0.1)

    def loop(self):
        try:
            self.loop = gobject.MainLoop()
            self.loop.run()
        except Exception, e:
            self.logger.exception("Encountered a problem in the MainLoop, tearing down the pipeline")
            self.pipeline.set_state(gst.STATE_NULL)

    def on_message(self, bus, message):
        if message.type == gst.MESSAGE_ELEMENT:
            if message.structure.get_name() == 'level':
                if self.started is False:
                    self.started = True
                    if len(message.structure['peak']) == 1:
                        self.logger.info("Started mono audio transmission")
                    else:
                        self.logger.info("Started stereo audio transmission")
        return True

    def get_caps(self):
        return self.caps
