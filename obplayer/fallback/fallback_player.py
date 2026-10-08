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
import sys
import time
import magic
import random
import threading
import traceback

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstPbutils", "1.0")
from gi.repository import GObject, Gst, GstPbutils

if sys.version.startswith("3"):
    unicode = str


class ObFallbackPlayer(obplayer.player.ObPlayerController):

    def __init__(self):
        self.media = []

        self.media_types = []
        self.image_types = []

        self.media_types.append("audio/x-flac")
        self.media_types.append("audio/flac")
        self.media_types.append("audio/mpeg")
        self.media_types.append("audio/ogg")
        self.media_types.append("audio/x-wav")
        self.media_types.append("audio/wav")

        # TODO we're always headless new so we never play images or video??
        # if obplayer.Config.headless == False:
        self.image_types.append("image/jpeg")
        self.image_types.append("image/png")
        self.image_types.append("image/svg+xml")
        self.media_types.append("application/ogg")
        self.media_types.append("video/ogg")
        self.media_types.append("video/x-msvideo")
        self.media_types.append("video/mp4")
        self.media_types.append("video/mpeg")

        self.play_index = 0
        self.image_duration = 15.0

        # guards play_index and the media order between the player thread (do_player_request)
        # and live assist's fallback controls (next/prev/play)
        self.lock = threading.Lock()

        # wall-clock time a queued request last ended *naturally* (set via onend, which
        # does not fire on preemption); used to tell a fresh re-engagement (a real gap
        # after a show) apart from continuing the fallback rotation.
        self.last_request_end = 0
        self.booted = False
        self.engage_delay = 1.0
        # longer hold on the very first engagement (player boot) to let everything init.
        self.boot_engage_delay = 10.0

        m = magic.open(magic.MAGIC_MIME)
        m.load()

        for dirname, dirnames, filenames in os.walk(
            unicode(obplayer.Config.setting("fallback_media"))
        ):
            for filename in filenames:
                try:
                    path = os.path.join(dirname, filename)
                    uri = obplayer.Player.file_uri(path)
                    filetype = m.file(path.encode("utf-8")).split(";")[0]

                    if filetype in self.media_types:
                        d = GstPbutils.Discoverer()
                        mediainfo = d.discover_uri(uri)

                        media_type = None
                        for stream in mediainfo.get_video_streams():
                            if stream.is_image():
                                media_type = "image"
                            else:
                                media_type = "video"
                                break
                        if not media_type and len(mediainfo.get_audio_streams()) > 0:
                            media_type = "audio"

                        if media_type:
                            # we discovered some more fallback media, add to our media list.
                            (artist, title) = self.track_names(mediainfo, filename)
                            self.media.append(
                                [
                                    uri,
                                    filename,
                                    media_type,
                                    float(mediainfo.get_duration()) / Gst.SECOND,
                                    artist,
                                    title,
                                ]
                            )

                    if filetype in self.image_types:
                        self.media.append(
                            [
                                uri,
                                filename,
                                "image",
                                self.image_duration,
                                "",
                                os.path.splitext(filename)[0],
                            ]
                        )
                except:
                    obplayer.Log.log(
                        "exception while loading fallback media: "
                        + dirname
                        + "/"
                        + filename,
                        "error",
                    )
                    obplayer.Log.log(traceback.format_exc(), "error")

        # shuffle the list
        random.shuffle(self.media)

        # allow_requeue=False so that we can add a gap each time fallback player is re-engaged
        self.ctrl = obplayer.Player.create_controller(
            "fallback", priority=25, allow_requeue=False
        )
        self.ctrl.set_request_callback(self.do_player_request)

    # called when a queued request ends naturally (the player does not call this on
    # preemption). Records the real end time so a request that was cut short early
    # leaves last_request_end in the past, correctly triggering the engage delay.
    def mark_request_end(self):
        self.last_request_end = time.time()

    # artist and title from the file's tags, else from an "Artist - Title.mp3" filename
    def track_names(self, mediainfo, filename):
        artist = title = None
        tags = mediainfo.get_tags()
        if tags is not None:
            (found, value) = tags.get_string(Gst.TAG_ARTIST)
            if found:
                artist = value
            (found, value) = tags.get_string(Gst.TAG_TITLE)
            if found:
                title = value

        if not title:
            name = os.path.splitext(filename)[0]
            if " - " in name:
                (name_artist, title) = name.split(" - ", 1)
                artist = artist or name_artist
            else:
                title = name

        return (unicode(artist or ""), unicode(title))

    # the player is asking us what to play next
    def do_player_request(self, ctrl, present_time, media_class):
        with self.lock:
            if len(self.media) == 0:
                return False

            # If we're re-engaging after a gap (a show just ended, or a higher-priority
            # source dropped out) rather than continuing the rotation, hold silence for a
            # moment first so the next show or an override can take over without a fallback
            # blip. Mid-rotation track changes have present_time ~= last_request_end, so
            # they skip this and play back-to-back.
            if present_time - self.last_request_end > 0.5:
                # use the longer hold on the very first engagement (player boot).
                delay = self.engage_delay if self.booted else self.boot_engage_delay
                self.booted = True
                ctrl.add_request(
                    media_type="break",
                    duration=delay,
                    title="fallback engage delay",
                    onend=self.mark_request_end,
                )
                return True

            self.queue_track(ctrl)
            return True

    # queue the track at play_index and move past it. Reshuffles once the whole rotation has played.
    def queue_track(self, ctrl, start_time=None):
        if self.play_index >= len(self.media):
            self.play_index = 0
            random.shuffle(
                self.media
            )  # shuffle again to create a new order for next time.

        media = self.media[self.play_index]
        ctrl.add_request(
            media_type=unicode(media[2]),
            start_time=start_time,
            uri=unicode(media[0]),
            duration=media[3],
            order_num=self.play_index,
            artist=media[4],
            title=media[5],
            onend=self.mark_request_end,
        )

        self.play_index = self.play_index + 1

    #
    # Live assist controls: jump around the rotation while the fallback is on air
    #

    # is the fallback what's on air (rather than a show or an override)?
    def on_air(self):
        return len(self.ctrl.player.get_controller_requests(self.ctrl)) > 0

    # rotation position of the track on air, or -1
    def current_index(self):
        if not self.on_air():
            return -1
        return self.play_index - 1

    def get_queue(self):
        with self.lock:
            items = [
                {
                    "artist": media[4],
                    "title": media[5],
                    "duration": media[3],
                    "media_type": media[2],
                }
                for media in self.media
            ]
        return {"current": self.current_index(), "items": items}

    def play(self, index):
        if not 0 <= index < len(self.media):
            return False
        return self.skip_to(index)

    def next(self):
        return self.skip_to(self.play_index)

    def previous(self):
        return self.skip_to(max(0, self.play_index - 2))

    # cut what the fallback has on air (with the short pause fade) and play the track at index
    # (len(self.media) starts a fresh shuffle)
    def skip_to(self, index):
        with self.lock:
            if len(self.media) == 0 or not self.on_air():
                return False

            # queue the new track to start now, before the fade frees the output, so the player
            # picks it up straight away rather than asking us for a track (and adding the engage
            # delay, as it would after a gap)
            self.ctrl.clear_queue()
            self.play_index = index
            self.queue_track(self.ctrl, start_time=time.time())

            fade = float(obplayer.Config.setting("pause_fade"))
            if fade > 0:
                obplayer.Player.fade_out_controller_requests(self.ctrl, fade)
            else:
                for output in obplayer.Player.get_controller_requests(self.ctrl):
                    obplayer.Player.stop_request(output)
            obplayer.Player.request_update.set()
        return True
