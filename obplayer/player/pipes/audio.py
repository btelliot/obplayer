#!/usr/bin/python3
# -*- coding: utf-8 -*-

"""
Copyright 2012-2024 OpenBroadcaster, Inc.

This file is part of OpenBroadcaster Player.

OpenBroadcaster Player is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

OpenBroadcaster Player is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with OpenBroadcaster Player.  If not, see <http://www.gnu.org/licenses/>.
"""

import obplayer

import os
import threading
import traceback

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GObject, Gst, GstVideo, GstController

from .base import ObGstPipeline


class ObAudioPipeline(ObGstPipeline):
    def __init__(self, name, player):
        ObGstPipeline.__init__(self, name)

        self.player = player

        # make the rest of the code happy by having a pipeline all the time
        self.pipeline = Gst.parse_launch("audiotestsrc ! audioconvert ! fakesink")
        self.pipeline.set_state(Gst.State.PAUSED)

    def set_request(self, req):
        self.mixer_off()

        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
        self.pipeline = False

        if req["uri"]:
            # remove file:// from req['uri']
            if req["uri"].startswith("file://"):
                req["uri"] = req["uri"][7:]

            self.pipeline = Gst.parse_launch(
                "filesrc name=filesrc ! decodebin ! audioconvert ! audioresample ! capsfilter caps=audio/x-raw,format=S16LE,rate=44100,layout=interleaved,channels=2 ! interpipesink sync=true name="
                + self.interpipe_name
            )
            self.pipeline.get_by_name("filesrc").set_property("location", req["uri"])

        else:
            self.pipeline = Gst.parse_launch("audiotestsrc ! audioconvert ! fakesink")

        self.pipeline.set_state(Gst.State.PAUSED)
        self.pipeline.get_state(Gst.CLOCK_TIME_NONE)

        self.mixer_on()


class ObAlertPipeline(ObAudioPipeline):

    min_class = ["alert"]
    max_class = ["alert"]
    interpipe_name = "interpipe-alert"

    def mixer_on(self):
        self.player.outputs["mixer"].alert_on()

    def mixer_off(self):
        self.player.outputs["mixer"].alert_off()


class ObVoicetrackPipeline(ObAudioPipeline):

    min_class = ["voicetrack"]
    max_class = ["voicetrack"]
    interpipe_name = "interpipe-voicetrack"

    def mixer_on(self):
        self.player.outputs["mixer"].voicetrack_on()

    def mixer_off(self):
        self.player.outputs["mixer"].voicetrack_off()


# Live assist carts: sound effects fired over whatever is on air, into their own mixer channel.
# Not a request pipe, so a cart never stops, holds or changes the playing track (or a pause).
# One cart at a time: firing another replaces the one still playing.
class ObCartPlayer(object):
    def __init__(self, player):
        self.player = player
        self.lock = threading.Lock()
        self.pipeline = None
        self.handlers = []
        self.count = 0

    def play(self, uri, title=""):
        with self.lock:
            self.stop_pipeline()
            # a fresh interpipe name per cart: an ended cart's sink can linger under the old
            # name, and the mixer input would attach to it instead of the new cart
            self.count += 1
            name = "interpipe-cart-" + str(self.count)
            pipeline = Gst.parse_launch(
                "uridecodebin name=src ! audioconvert ! audioresample ! capsfilter name=caps ! interpipesink sync=true name="
                + name
            )
            pipeline.get_by_name("src").set_property("uri", uri)
            pipeline.get_by_name("caps").set_property(
                "caps", Gst.Caps.from_string(obplayer.Config.setting("audio_caps"))
            )
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            self.handlers = [
                bus.connect("message::eos", self.on_done, pipeline),
                bus.connect("message::error", self.on_done, pipeline),
            ]

            pipeline.set_state(Gst.State.PAUSED)
            pipeline.get_state(Gst.CLOCK_TIME_NONE)
            self.player.outputs["mixer"].cart_on(name)
            pipeline.set_state(Gst.State.PLAYING)
            self.pipeline = pipeline
            obplayer.Log.log("cart: playing " + title, "player")

    def stop(self):
        with self.lock:
            self.stop_pipeline()

    def stop_pipeline(self):
        if self.pipeline is None:
            return
        self.player.outputs["mixer"].cart_off()
        bus = self.pipeline.get_bus()
        for handler in self.handlers:
            bus.disconnect(handler)
        self.handlers = []
        bus.remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
        self.pipeline = None

    def on_done(self, bus, message, pipeline):
        if message.type == Gst.MessageType.ERROR:
            obplayer.Log.log("cart: " + str(message.parse_error()[0].message), "error")
        with self.lock:
            # a newer cart may already have replaced this one
            if pipeline is self.pipeline:
                self.stop_pipeline()
