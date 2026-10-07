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
import math
import time
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .base import ObGstPipeline


# A/B decks for audio tracks.
#
# Each track plays on whichever deck is idle, so playback alternates A, B, A, B. During a
# crossfade the outgoing deck keeps playing (and fading) while the incoming deck starts.
#
#   deck A playbin -> interpipe-deck-a -> volume -.
#                                                  +-> audiomixer -> audio output bin (shared)
#   deck B playbin -> interpipe-deck-b -> volume -'
#
# The decks are mixed before the shared audio output bin, so everything after it (output volume,
# level meters, stream taps, the main mixer with voicetracks, alerts and show fades) is unchanged.
# To the rest of the player this is still a single "audio" pipe.


class ObAudioDeck(object):
    def __init__(self, pipe, label):
        self.pipe = pipe
        self.label = label
        self.interpipe_name = "interpipe-deck-" + label.lower()

        self.req = None
        self.generation = 0
        self.fade_cancel = None

        self.playbin = Gst.ElementFactory.make("playbin", "audio-deck-" + label.lower())
        self.playbin.set_property("flags", 0x00000002 | 0x00000010)  # audio, soft volume

        sinkbin = Gst.Bin.new("audio-deck-" + label.lower() + "-sink")
        elements = [
            Gst.ElementFactory.make("audioconvert"),
            Gst.ElementFactory.make("audioresample"),
            Gst.ElementFactory.make("capsfilter"),
            Gst.ElementFactory.make("interpipesink", self.interpipe_name),
        ]
        elements[2].set_property(
            "caps", Gst.Caps.from_string(obplayer.Config.setting("audio_caps"))
        )
        elements[3].set_property("sync", True)
        for element in elements:
            sinkbin.add(element)
        for index in range(len(elements) - 1):
            elements[index].link(elements[index + 1])
        sinkbin.add_pad(Gst.GhostPad.new("sink", elements[0].get_static_pad("sink")))

        self.playbin.set_property("audio-sink", sinkbin)
        self.playbin.set_property("video-sink", Gst.ElementFactory.make("fakesink"))

        bus = self.playbin.get_bus()
        bus.add_signal_watch()
        bus.connect("message", pipe.message_handler)

    def wait_state(self, target_state):
        self.playbin.set_state(target_state)
        (statechange, state, pending) = self.playbin.get_state(timeout=5 * Gst.SECOND)
        if statechange != Gst.StateChangeReturn.SUCCESS:
            obplayer.Log.log(
                "deck "
                + self.label
                + ": gstreamer failed waiting for state change to "
                + str(target_state),
                "error",
            )
            return False
        return True

    def is_playing(self):
        (change, state, pending) = self.playbin.get_state(0)
        return state == Gst.State.PLAYING


class ObAudioDeckPipeline(ObGstPipeline):
    min_class = ["audio"]
    # claim visual like the old audio playbin did (it shows nothing; audio visualization isn't supported)
    max_class = ["audio", "visual"]

    fade_steps_per_second = 50

    def __init__(self, name, player):
        ObGstPipeline.__init__(self, name)
        self.player = player
        self.lock = threading.RLock()

        self.decks = [ObAudioDeck(self, "A"), ObAudioDeck(self, "B")]
        self.active = None  # deck holding the newest request
        self.cued = None  # deck loaded by set_request(), waiting for start()

        # mix pipeline: one channel per deck, listening to silence while the deck is idle
        self.pipeline = Gst.Pipeline.new(name + "-mix")
        self.mixer = Gst.ElementFactory.make("audiomixer", name + "-mixer")
        self.pipeline.add(self.mixer)

        self.sources = []
        self.volumes = []
        for deck in self.decks:
            src = Gst.ElementFactory.make("interpipesrc", "interpipesrc-deck-" + deck.label.lower())
            src.set_property("listen-to", "interpipe-none")
            src.set_property("is-live", True)
            src.set_property("format", Gst.Format.TIME)
            Gst.util_set_object_arg(src, "stream-sync", "restart-ts")
            volume = Gst.ElementFactory.make("volume", "deck-" + deck.label.lower() + "-volume")
            chain = [
                src,
                Gst.ElementFactory.make("queue"),
                Gst.ElementFactory.make("audioconvert"),
                Gst.ElementFactory.make("audioresample"),
                volume,
            ]
            for element in chain:
                self.pipeline.add(element)
            for index in range(len(chain) - 1):
                chain[index].link(chain[index + 1])
            volume.link(self.mixer)
            self.sources.append(src)
            self.volumes.append(volume)

        self.mixconvert = Gst.ElementFactory.make("audioconvert", name + "-mix-convert")
        self.pipeline.add(self.mixconvert)
        self.mixer.link(self.mixconvert)

        self.fakesink = Gst.ElementFactory.make("fakesink", name + "-fakesink")
        self.audiosink = None
        self.set_audio_sink(self.fakesink)

        self.register_signals()

    #
    # output patching (same approach as ObBreakPipeline)
    #
    def set_audio_sink(self, sink):
        if self.audiosink:
            self.mixconvert.unlink(self.audiosink)
            self.pipeline.remove(self.audiosink)
        self.audiosink = sink
        self.pipeline.add(self.audiosink)
        self.mixconvert.link(self.audiosink)

    def patch(self, mode):
        obplayer.Log.log(self.name + ": patching " + mode, "debug")
        with self.lock:
            if "audio" in mode.split("/") and "audio" not in self.mode:
                self.wait_state(Gst.State.NULL)
                self.set_audio_sink(self.player.outputs["audio"].get_bin())
            ObGstPipeline.patch(self, mode)
            if "audio" in self.mode:
                self.wait_state(Gst.State.PLAYING)

    def unpatch(self, mode):
        obplayer.Log.log(self.name + ": unpatching " + mode, "debug")
        with self.lock:
            if "audio" in mode.split("/") and "audio" in self.mode:
                self.wait_state(Gst.State.NULL)
                self.set_audio_sink(self.fakesink)
            ObGstPipeline.unpatch(self, mode)
            if "audio" in self.mode:
                self.wait_state(Gst.State.PLAYING)

    #
    # playback
    #
    def idle_deck(self):
        if self.active is None:
            for index, deck in enumerate(self.decks):
                if not deck.is_playing():
                    return index
            return 0
        return 1 - self.active

    def deck_for(self, req):
        for index, deck in enumerate(self.decks):
            if deck.req is req:
                return index
        return None

    def set_request(self, req):
        with self.lock:
            index = self.idle_deck()
            deck = self.decks[index]
            self.stop_deck(index)

            deck.req = req
            deck.playbin.set_property("uri", req["uri"])
            deck.wait_state(Gst.State.PAUSED)

            if obplayer.Config.setting("gst_init_callback"):
                os.system(obplayer.Config.setting("gst_init_callback"))

            offset = time.time() - req["start_time"]
            if offset > 0.05:
                if not deck.playbin.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                    int(offset * Gst.SECOND),
                ):
                    obplayer.Log.log("unable to seek on this track", "error")
                if offset > 0.25:
                    obplayer.Log.log(
                        "resuming track at " + str(round(offset, 3)) + " seconds.",
                        "player",
                    )
                deck.wait_state(Gst.State.PAUSED)

            self.cued = index

    def start(self):
        with self.lock:
            if "audio" in self.mode:
                self.wait_state(Gst.State.PLAYING)

            index = self.cued
            if index is None:
                return
            self.cued = None
            deck = self.decks[index]
            req = deck.req

            # only fade in under an outgoing deck; with nothing playing out (a hard cut, "stop after
            # this track", a restart) start at full volume
            fade_in = req.get("fade_in", 0) or 0
            if not self.decks[1 - index].is_playing():
                fade_in = 0

            if fade_in > 0 and time.time() < req["start_time"] + fade_in:
                self.ramp(index, req["start_time"], req["start_time"] + fade_in, "in")
            else:
                self.volumes[index].set_property("volume", 1.0)

            deck.wait_state(Gst.State.PLAYING)
            self.sources[index].set_property("listen-to", deck.interpipe_name)
            self.active = index

            # the main mixer channel listens to silence until a track starts (the old audio playbin
            # toggled it around every seek); only switch it when needed so a playing deck isn't disturbed
            mixer = self.player.outputs["mixer"]
            if (
                mixer.pipeline_main.get_by_name("interpipesrc-main").get_property("listen-to")
                != "interpipe-main"
            ):
                mixer.main_on()

            obplayer.Log.log(
                "deck "
                + deck.label
                + ": playing"
                + (" (fade in " + str(round(fade_in, 2)) + "s)" if fade_in > 0 else ""),
                "player",
            )

    # stop both decks (pause, preemption, a track ending without a crossfade)
    def stop(self, label=""):
        obplayer.Log.log(self.name + ": stopped " + label, "debug")
        with self.lock:
            for index in range(len(self.decks)):
                self.stop_deck(index)
            self.active = None
            self.cued = None

    # starting the next request only touches the idle deck (set_request stops it), so the other deck can play out
    def cue_stop(self, label=""):
        pass

    def stop_deck(self, index):
        deck = self.decks[index]
        deck.generation += 1
        self.cancel_ramp(index)
        self.sources[index].set_property("listen-to", "interpipe-none")
        deck.wait_state(Gst.State.NULL)
        deck.req = None
        if self.active == index:
            self.active = None
        if self.cued == index:
            self.cued = None

    def quit(self):
        with self.lock:
            for index in range(len(self.decks)):
                self.stop_deck(index)
        self.wait_state(Gst.State.NULL)

    def is_playing(self):
        return any(deck.is_playing() for deck in self.decks)

    # used while adding stream taps, which needs the output bin's pipeline stopped
    def stop_output(self):
        with self.lock:
            self.wait_state(Gst.State.NULL)

    def start_output(self):
        with self.lock:
            if "audio" in self.mode:
                self.wait_state(Gst.State.PLAYING)

    #
    # crossfades
    #
    # fade the deck playing req out between start_time and end_time (equal-power cosine curve)
    def fade_out(self, req, start_time, end_time):
        with self.lock:
            index = self.deck_for(req)
            if index is None:
                return
            obplayer.Log.log(
                "deck "
                + self.decks[index].label
                + ": fade out "
                + str(round(end_time - start_time, 2))
                + "s",
                "player",
            )
            self.ramp(index, start_time, end_time, "out")

    # let the deck playing req carry on alone until stop_at, then stop it; the output is free for the next request
    def release(self, req, stop_at):
        with self.lock:
            index = self.deck_for(req)
            if index is None:
                return
            deck = self.decks[index]
            generation = deck.generation

        def stop_when_done():
            time.sleep(max(0, stop_at - time.time()) + 0.25)
            with self.lock:
                if deck.generation == generation:
                    self.stop_deck(index)

        threading.Thread(target=stop_when_done, daemon=True).start()

    # put a deck back to full volume (e.g. "stop after this track" switched on mid-fade)
    def cancel_fade(self, req):
        with self.lock:
            index = self.deck_for(req)
            if index is not None:
                self.cancel_ramp(index)
                self.volumes[index].set_property("volume", 1.0)

    def ramp(self, index, start_time, end_time, direction):
        self.cancel_ramp(index)
        cancel = threading.Event()
        self.decks[index].fade_cancel = cancel
        volume = self.volumes[index]
        length = max(end_time - start_time, 0.001)

        def level(now):
            progress = min(max((now - start_time) / length, 0.0), 1.0)
            if direction == "in":
                return math.sin(progress * math.pi / 2)
            return math.cos(progress * math.pi / 2)

        volume.set_property("volume", level(time.time()))

        def run():
            while not cancel.is_set():
                now = time.time()
                volume.set_property("volume", level(now))
                if now >= end_time:
                    break
                cancel.wait(1.0 / self.fade_steps_per_second)

        threading.Thread(target=run, daemon=True).start()

    def cancel_ramp(self, index):
        if self.decks[index].fade_cancel is not None:
            self.decks[index].fade_cancel.set()
            self.decks[index].fade_cancel = None
