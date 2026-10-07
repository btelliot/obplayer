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

import time
import threading


class ObPlaylist(object):
    def __init__(self, show_id):
        self.pos = 0
        self.playlist = obplayer.RemoteData.get_show_media(show_id)
        self.voicetracks = obplayer.RemoteData.get_show_voicetracks(show_id)

        # how close to the voicetrack before we return it to queue playback?
        self.voicetrack_timing_tolerance = 0.25
        self.voicetrack_none_until = 0  # allow us to delay the next voicetrack return to prevent duplicate play requests

        if not self.voicetracks:
            self.voicetracks = []

        if not self.playlist:
            self.playlist = []

    def size(self):
        return len(self.playlist)

    def current_pos(self):
        if self.pos >= len(self.playlist):
            return len(self.playlist) - 1
        return self.pos

    def current(self):
        if self.pos >= len(self.playlist):
            return None
        return self.playlist[self.pos]

    # delay next voicetrack return to prevent duplicate play requests
    def delay_voicetrack(self):
        self.voicetrack_none_until = time.time() + self.voicetrack_timing_tolerance

    def current_voicetrack(self, current_track_position):
        if self.voicetrack_none_until > time.time():
            return None

        current = self.current()

        # voicetrack is always played relative to the current or next track
        if not current:
            return None

        track_ends_in = current["duration"] - current_track_position

        for voicetrack in self.voicetracks:
            # if the voicetrack is set relative to the current track, and has a positive delay, then it plays at the start of this track
            if voicetrack["order_num"] == self.pos and voicetrack["delay"] >= 0:
                delta = voicetrack["delay"] - current_track_position

                if (
                    delta >= -self.voicetrack_timing_tolerance
                    and delta <= self.voicetrack_timing_tolerance
                ):
                    self.delay_voicetrack()
                    return [voicetrack, delta]

            # if the voicetrack is set relative to the next track, but has a negative delay, then it plays at the end of this track
            if voicetrack["order_num"] == (self.pos + 1) and voicetrack["delay"] < 0:
                delta = track_ends_in - abs(voicetrack["delay"])
                if (
                    delta >= -self.voicetrack_timing_tolerance
                    and delta <= self.voicetrack_timing_tolerance
                ):
                    self.delay_voicetrack()
                    return [voicetrack, delta]

        return None

    def increment(self):
        self.pos += 1
        if self.pos >= len(self.playlist):
            return False
        return True

    def decrement(self):
        self.pos -= 1
        if self.pos < 0:
            self.pos = 0
            return False
        return True

    def set(self, pos):
        self.pos = pos
        if self.pos < 0:
            self.pos = 0
        elif self.pos > len(self.playlist):
            self.pos = len(self.playlist)

    def is_finished(self):
        if self.pos >= len(self.playlist):
            return True
        return False

    def is_last(self):
        if self.pos + 1 >= len(self.playlist):
            return True
        return False

    def next_start(self):
        if self.pos + 1 >= len(self.playlist):
            return None
        return self.playlist[self.pos + 1]["offset"]

    #
    # Work out crossfade transitions from observer's per-item crossfade values (seconds of overlap
    # with the next item; observer has already moved the next item's offset earlier by that amount).
    #
    # Sets on each item:
    #   fade_in   seconds to ramp up from silence at the start
    #   fade_out  seconds before the end to start fading out
    #   overlap   seconds before the end that the next item starts
    #
    # Station IDs are never faded. Observer doesn't mark them, so an item counts as one when it has no
    # crossfade of its own, follows a crossfaded track, and is shorter than crossfade_id_max_length.
    #   track -> track (X):  fade out X, next starts X before the end, fades in over X
    #   track -> ID (X):     fade out X, ID starts at full volume X/3 before the end (2/3 into the fade)
    #   ID -> track:         ID plays at full volume, next track fades in under its last 2X/3
    # Offsets are adjusted to match, which moves each ID 2X/3 later; the track after it lands back
    # on observer's offset.
    #
    def plan_transitions(self):
        enabled = obplayer.Config.setting("crossfade_enable")
        try:
            id_max_length = float(obplayer.Config.setting("crossfade_id_max_length"))
        except (TypeError, ValueError):
            id_max_length = 60.0

        items = self.playlist

        def is_audio(item):
            return item["media_type"] == "audio"

        for index, item in enumerate(items):
            item.setdefault("crossfade", 0.0)
            item["fade_in"] = 0.0
            item["fade_out"] = 0.0
            item["overlap"] = 0.0
            previous = items[index - 1] if index > 0 else None
            item["is_station_id"] = bool(
                previous
                and is_audio(item)
                and is_audio(previous)
                and item["crossfade"] <= 0
                and previous["crossfade"] > 0
                and item["duration"] < id_max_length
            )

        if enabled:
            for index in range(len(items) - 1):
                item = items[index]
                following = items[index + 1]
                if not is_audio(item) or not is_audio(following):
                    continue

                crossfade = min(item["crossfade"], item["duration"], following["duration"])
                if crossfade <= 0:
                    continue

                if not following["is_station_id"]:
                    item["fade_out"] = crossfade
                    item["overlap"] = crossfade
                    following["fade_in"] = crossfade
                    continue

                # track -> station ID
                item["fade_out"] = crossfade
                item["overlap"] = crossfade / 3

                # station ID -> track: fade the track in under the ID's end, but never start it
                # before the track ahead of the ID has finished. Not into another short item with
                # no crossfade (likely a second ID).
                after = items[index + 2] if index + 2 < len(items) else None
                if (
                    after
                    and is_audio(after)
                    and not (after["crossfade"] <= 0 and after["duration"] < id_max_length)
                ):
                    overlap = min(
                        crossfade * 2 / 3,
                        following["duration"] - crossfade / 3,
                        after["duration"],
                    )
                    if overlap > 0:
                        following["overlap"] = overlap
                        after["fade_in"] = overlap

        # observer subtracted each item's crossfade from the next item's offset; apply our own overlap instead
        shift = 0.0
        for index in range(1, len(items)):
            shift += items[index - 1]["crossfade"] - items[index - 1]["overlap"]
            items[index]["offset"] += shift

    def advance_to_current(self, present_offset, media_type=None, latest=False):
        # with crossfades two items can be current at once; latest picks the one that started last
        if latest:
            for i in reversed(range(0, len(self.playlist))):
                if (
                    present_offset + 0.05 >= self.playlist[i]["offset"]
                    and present_offset
                    <= self.playlist[i]["offset"] + self.playlist[i]["duration"]
                    and (not media_type or media_type == self.playlist[i]["media_type"])
                ):
                    self.pos = i
                    return True
            self.pos = len(self.playlist)
            return False

        for i in range(0, len(self.playlist)):
            if (
                present_offset >= self.playlist[i]["offset"]
                and present_offset
                <= self.playlist[i]["offset"] + self.playlist[i]["duration"]
                and (not media_type or media_type == self.playlist[i]["media_type"])
            ):
                self.pos = i
                return True
        self.pos = len(self.playlist)
        return False


class ObShow(object):
    # crossfaded items overlap; when finding the current item, use the one that started last
    prefer_latest_item = True

    def __init__(self):
        self.paused = False
        self.pause_position = 0
        self.auto_advance = True
        self.stop_after = False  # live assist: hold in a break when the current track ends

        self.media_start_time = 0
        self.now_playing = None
        self.next_media_update = 0
        self.fadeout = False

        self.show_data = None
        self.playlist = None
        self.groups = None

    @staticmethod
    def find_show(datetime):
        data = obplayer.RemoteData.get_present_show(datetime)
        if not data:
            return None

        # TODO we could possibly have a list of possible show types
        if data["type"] == "live_assist":
            self = ObLiveAssistShow()
        elif data["type"] == "advanced":
            self = ObAdvancedShow()
        else:
            self = ObShow()
        self.show_data = data
        self.playlist = ObPlaylist(self.show_data["id"])
        if data["type"] != "advanced":
            self.playlist.plan_transitions()
        self.groups = obplayer.RemoteData.load_groups(self.show_data["id"])

        return self

    def get_break_media(self, end_time=None, title="show paused break"):
        if end_time == None:
            end_time = self.end_time()
        return {
            "media_type": "break",
            "end_time": end_time,
            "artist": "[scheduler]",
            "title": title,
        }

    def id(self):
        return self.show_data["id"]

    def show_id(self):
        return self.show_data["show_id"]

    def name(self):
        return self.show_data["name"]

    def start_time(self):
        return self.show_data["start_time"]

    def end_time(self):
        return self.show_data["end_time"]

    def show_info(self):
        return {
            key: self.show_data[key]
            for key in self.show_data
            if key in ["id", "name", "description", "type", "last_updated"]
        }

    def get_playlist(self):
        return self.playlist.playlist

    def get_groups(self):
        return self.groups

    def start_show(self, present_time):
        self.ctrl.stop_requests()

        # find the track that should play at the present time
        self.playlist.advance_to_current(
            present_time - self.show_data["start_time"],
            latest=self.prefer_latest_item,
        )
        obplayer.Log.log(
            "starting at track number " + str(self.playlist.pos), "scheduler"
        )

        self.play_current(present_time)

    def play_next(self, present_time, media_class=None):
        # try advancing to the current track (must be done before checking is_finished since this is what advances the track position)
        if self.playlist.advance_to_current(
            present_time - self.start_time(), latest=self.prefer_latest_item
        ):
            # don't request the same track twice (both the player's request query and the scheduled
            # update can get here, and the outgoing track of a crossfade is no longer the player's request)
            if not self.ctrl.has_request_for(self.playlist.current()["order_num"]):
                self.play_current(present_time)

        if self.is_paused() or self.playlist.is_finished():
            self.ctrl.stop_requests()
            self.ctrl.add_request(
                media_type="break", end_time=self.end_time(), title="show paused break"
            )
            return False

        if self.ctrl.has_requests():
            return False
        # TODO why was this here?
        # print("This is a bug...")
        self.media_start_time = 0

        # NOTE this usually only happens when a media item fails to play, in which case we don't want to fill the
        # space with a pause, we want to let the fallback player take over
        # self.ctrl.stop_requests()
        # self.ctrl.add_request(media_type='break', end_time=self.end_time(), title = "break (filling spaces)")

    def play_current_voicetrack(self, present_time):
        voicetrack = self.playlist.current_voicetrack(
            present_time - self.media_start_time
        )
        if voicetrack:
            self.play_voicetrack(voicetrack[0], voicetrack[1], present_time)

    def play_current(self, present_time):
        if self.is_paused():
            self.ctrl.stop_requests()
            self.ctrl.add_request(
                media_type="break", end_time=self.end_time(), title="show paused break"
            )
            return False

        media = self.playlist.current()

        if not media or not obplayer.Sync.check_media(media):
            obplayer.Log.log(
                (
                    "media not found at position "
                    + str(self.playlist.current_pos())
                    + ": "
                    + str(media["filename"])
                    if media
                    else "??"
                ),
                "scheduler",
            )
            next_start = self.playlist.next_start() if media else None
            self.next_media_update = (
                self.start_time() + next_start if next_start else self.end_time()
            )
            # self.ctrl.stop_requests()
            # self.ctrl.add_request(media_type='break', duration=2, title="media not found break")
            return False

        offset = present_time - self.show_data["start_time"] - media["offset"]
        self.play_media(media, offset, present_time)
        next_start = self.playlist.next_start()
        self.next_media_update = (
            self.start_time() + next_start if next_start else self.end_time()
        )

        return True

    def play_voicetrack(self, voicetrack_media, delay, present_time):
        duration_with_fade_time = (
            voicetrack_media["duration"] + voicetrack_media["fadeout"]
        )

        self.voicetrack_ctrl.add_request(
            start_time=present_time + delay,
            media_type="voicetrack",
            uri=obplayer.Sync.media_uri(
                voicetrack_media["file_location"], voicetrack_media["filename"]
            ),
            media_id=voicetrack_media["media_id"],
            order_num=voicetrack_media["order_num"],
            artist="voicetrack",
            title="voicetrack",
            duration=duration_with_fade_time,
            mixerstart=[
                "voicetrack_on",
                {
                    "fade": voicetrack_media["fadeout"],
                    "volume": voicetrack_media["volume"],
                },
            ],
            mixerend=["voicetrack_off", {"fade": voicetrack_media["fadein"]}],
            padstart=voicetrack_media["fadeout"],
        )

    # crossfade values for a media item's request (see ObPlaylist.plan_transitions)
    def transition_for(self, media, fade_in=None):
        if media["media_type"] != "audio" or not obplayer.Config.setting(
            "crossfade_enable"
        ):
            return {}

        transition = {
            "fade_in": media.get("fade_in", 0) if fade_in is None else fade_in,
            "fade_out": media.get("fade_out", 0),
            "overlap": media.get("overlap", 0),
        }

        # live assist "stop after this track": play it out, no crossfade into the next one
        if self.stop_after:
            transition["fade_out"] = 0
            transition["overlap"] = 0

        return transition

    def play_media(self, media, offset, present_time, fade_in=None, fade_in_resume=False):

        self.now_playing = media
        self.pause_position = 0
        self.media_start_time = present_time - offset

        if media["media_type"] == "breakpoint":
            obplayer.Log.log(
                "stopping on breakpoint at position " + str(self.playlist.pos),
                "scheduler",
            )
            self.playlist.increment()
            self.auto_advance = False
            self.media_start_time = 0
            obplayer.Sync.now_playing_update(
                self.show_data["show_id"],
                self.show_data["end_time"],
                "",
                "",
                self.show_data["name"],
            )

            self.ctrl.add_request(
                media_type="break",
                end_time=self.end_time(),
                title="live assist breakpoint",
                order_num=media["order_num"],
            )

        else:
            fadeout_mode = self.show_data["last_track_fadeout"]
            fadeout = False

            # fade out if media is the last track or media ends at/after the show end time
            if (
                fadeout_mode == "always"
                and self.end_time()
                and (
                    self.playlist.is_last()
                    or self.media_start_time + media["duration"] >= self.end_time()
                )
            ):
                fadeout = True

            # fade out if media ends after the show end time
            if (
                fadeout_mode == "auto"
                and self.end_time()
                and self.media_start_time + media["duration"] > self.end_time()
            ):
                fadeout = True

            transition = self.transition_for(media, fade_in)
            if transition and fade_in_resume:
                transition["fade_in_resume"] = True
            # tracks cut or faded by the end of the show only keep their fade in
            fade_in_only = {
                key: transition[key]
                for key in ("fade_in", "fade_in_resume")
                if key in transition
            }

            if fadeout:
                self.fadeout = True
                self.ctrl.add_request(
                    start_time=self.media_start_time,
                    end_time=self.end_time(),
                    media_type=media["media_type"],
                    uri=obplayer.Sync.media_uri(
                        media["file_location"], media["filename"]
                    ),
                    media_id=media["media_id"],
                    order_num=media["order_num"],
                    artist=media["artist"],
                    title=media["title"],
                    mixerend=[
                        "primary_on",
                        {},
                    ],  # restore the mixer from fade-out when the track stops
                    **fade_in_only,
                )
            else:
                self.fadeout = False

                # if track does not end in time, use show end_time instead of track duration
                if self.end_time() and self.media_start_time + media['duration'] > self.end_time():
                    self.ctrl.add_request(
                        start_time = self.media_start_time,
                        end_time = self.end_time(),
                        media_type = media["media_type"],
                        uri = obplayer.Sync.media_uri(media["file_location"], media["filename"]),
                        media_id = media["media_id"],
                        order_num = media["order_num"],
                        artist = media["artist"],
                        title = media["title"],
                        **fade_in_only,
                    )
                else:
                    self.ctrl.add_request(
                        start_time = self.media_start_time,
                        media_type = media["media_type"],
                        uri = obplayer.Sync.media_uri(media["file_location"], media["filename"]),
                        media_id = media["media_id"],
                        order_num = media["order_num"],
                        artist = media["artist"],
                        title = media["title"],
                        duration = media["duration"],
                        **transition,
                    )

            obplayer.Sync.now_playing_update(
                self.show_data["show_id"],
                self.show_data["end_time"],
                media["media_id"],
                self.media_start_time + media["duration"],
                self.show_data["name"],
            )

    def playlist_seek(self, track_num, seek):
        self.playlist.set(track_num)
        self.paused = False
        self.auto_advance = True
        if not self.playlist.is_finished():
            media = self.playlist.current()
            # seeking into a track (e.g. the v2 seek bar) is a straight jump; only starting a
            # track from the top crossfades
            fade = self.skip_crossfade(media) if seek == 0 else 0
            if fade > 0:
                # live assist next/jump: crossfade from the playing track using its own crossfade length
                self.ctrl.clear_queue()
                obplayer.Player.fade_out_controller_requests(self.ctrl, fade)
                self.play_media(
                    media,
                    media["duration"] * (seek / 100),
                    time.time(),
                    fade_in=0 if media.get("is_station_id") else fade,
                )
            else:
                self.ctrl.stop_requests()
                self.play_media(media, media["duration"] * (seek / 100), time.time())

    # seconds left of the playing track if the operator can fade it (live assist, crossfades on,
    # and the scheduler's audio actually on the audio output), otherwise 0
    def fadeable_remaining(self):
        if not isinstance(self, ObLiveAssistShow) or not obplayer.Config.setting(
            "crossfade_enable"
        ):
            return 0

        playing = self.now_playing
        if (
            playing is None
            or self.media_start_time == 0
            or playing["media_type"] != "audio"
        ):
            return 0

        req = obplayer.Player.requests["audio"]
        if req is None or req["controller"] != self.ctrl or req["media_type"] != "audio":
            return 0

        return max(0, self.media_start_time + playing["duration"] - time.time())

    # seconds to crossfade when the operator skips to media, or 0 for a hard cut
    def skip_crossfade(self, media):
        remaining = self.fadeable_remaining()
        if remaining <= 0 or media["media_type"] != "audio":
            return 0
        return max(
            0, min(self.now_playing.get("crossfade", 0), remaining, media["duration"])
        )

    # seconds to fade out on pause, or 0 to stop dead
    def pause_fade(self):
        return min(obplayer.Config.setting("pause_fade"), self.fadeable_remaining())

    # seconds to fade back in when resuming media at offset, or 0 to start at full volume
    def resume_fade(self, media, offset):
        if (
            not isinstance(self, ObLiveAssistShow)
            or not obplayer.Config.setting("crossfade_enable")
            or media["media_type"] != "audio"
        ):
            return 0
        return max(0, min(obplayer.Config.setting("pause_fade"), media["duration"] - offset))

    def play_group_item(self, group_num, group_item_num, seek):
        if self.show_data["type"] != "live_assist":
            return False

        if group_num < 0 or group_num >= len(self.groups):
            return False
        group = self.groups[group_num]["items"]

        if group_item_num < 0 or group_item_num >= len(group):
            return False

        media = group[group_item_num]
        self.ctrl.stop_requests()
        self.play_media(media, media["duration"] * (seek / 100), time.time())
        self.paused = False
        self.auto_advance = False
        return True

    def pause(self, syncing=False):
        if not self.paused:
            fade = 0 if syncing else self.pause_fade()
            now = time.time()
            self.paused = True
            # the position Pause was pressed at, so resuming replays the faded-out tail
            self.pause_position = now - self.media_start_time
            self.media_start_time = 0
            if fade > 0:
                self.ctrl.clear_queue()
                break_start = obplayer.Player.fade_stop_controller_requests(self.ctrl, fade)
            else:
                self.ctrl.stop_requests()
                break_start = now
            if syncing:
                self.ctrl.stop_requests()
            else:
                # the break waits for the fade: starting it repatches the output and cuts the fade off
                self.ctrl.add_request(
                    media_type="break",
                    start_time=break_start,
                    end_time=self.end_time(),
                    title="show paused break",
                )

    def unpause(self):
        if self.paused:
            self.paused = False
            self.ctrl.stop_requests()
            if self.now_playing is None:
                self.play_current(time.time())
            else:
                fade = self.resume_fade(self.now_playing, self.pause_position)
                self.play_media(
                    self.now_playing,
                    self.pause_position,
                    time.time(),
                    fade_in=fade if fade > 0 else None,
                    fade_in_resume=fade > 0,
                )
                self.pause_position = 0
        elif not self.auto_advance:
            self.auto_advance = True
            self.ctrl.stop_requests()
            self.play_current(time.time())

    def next(self):
        pos = self.playlist.current_pos() + 1
        if pos < self.playlist.size():
            self.playlist_seek(pos, 0)

    def previous(self):
        pos = self.playlist.current_pos() - 1
        if pos >= 0:
            self.playlist_seek(pos, 0)

    def is_paused(self):
        return self.paused or not self.auto_advance

    def position(self):
        if self.media_start_time == 0:
            if self.now_playing == None:
                return 0
            return (
                self.pause_position
                if self.pause_position
                else self.now_playing["duration"]
            )
        return time.time() - self.media_start_time


class ObLiveAssistShow(ObShow):
    def start_show(self, present_time):
        # Already changed in another branch. Should be here too.
        # self.ctrl.stop_requests()

        obplayer.Log.log("starting live assist show", "scheduler")

        self.play_current(present_time)

    def play_next(self, present_time, media_class=None):
        if self.is_paused() or self.playlist.is_finished():
            # already holding a break (a breakpoint, "stop after this track", or pause): keep it.
            # The player can ask again in the same pass for its other outputs, and replacing the
            # "live assist breakpoint" break would hide the break from the live assist UI.
            if self.ctrl.has_requests():
                return False
            self.ctrl.stop_requests()
            self.ctrl.add_request(
                media_type="break", end_time=self.end_time(), title="show paused break"
            )
            return False

        if self.ctrl.has_requests():
            return False

        # "stop after this track": step past the finished track and hold like a playlist
        # breakpoint does, so Play (unpause) starts the next track rather than replaying this one.
        if self.stop_after:
            self.stop_after = False
            obplayer.Log.log(
                "stopping after track at position " + str(self.playlist.pos),
                "scheduler",
            )
            self.playlist.increment()
            self.auto_advance = False
            self.media_start_time = 0
            obplayer.Sync.now_playing_update(
                self.show_data["show_id"],
                self.show_data["end_time"],
                "",
                "",
                self.show_data["name"],
            )
            self.ctrl.stop_requests()
            self.ctrl.add_request(
                media_type="break",
                end_time=self.end_time(),
                title="live assist breakpoint",
            )
            return False

        # increment before checking if finished (otherwise finished is never detected)
        self.playlist.increment()
        if self.playlist.is_finished():
            self.ctrl.stop_requests()
            self.ctrl.add_request(
                media_type="break", end_time=self.end_time(), title="show paused break"
            )
            return False

        # TODO can you insert a break if the previous track failed to play?
        self.play_current(present_time)

    def play_current(self, present_time):
        if self.is_paused():
            self.ctrl.stop_requests()
            self.ctrl.add_request(
                media_type="break", end_time=self.end_time(), title="show paused break"
            )
            return False

        media = self.playlist.current()
        if not media or not obplayer.Sync.check_media(media):
            obplayer.Log.log(
                (
                    "media not found at position "
                    + str(self.playlist.current_pos())
                    + ": "
                    + str(media["filename"])
                    if media
                    else "??"
                ),
                "scheduler",
            )
            # self.ctrl.stop_requests()
            # self.ctrl.add_request(media_type='break', duration=2, title="media not found break")
            return False

        self.play_media(media, 0, present_time)
        return True

    def play_group_item(self, group_num, group_item_num, seek):
        if group_num < 0 or group_num >= len(self.groups):
            return False
        group = self.groups[group_num]["items"]

        if group_item_num < 0 or group_item_num >= len(group):
            return False

        media = group[group_item_num]
        self.ctrl.stop_requests()
        self.play_media(media, media["duration"] * (seek / 100), time.time())
        self.paused = False
        self.auto_advance = False
        return True


class ObAdvancedShow(ObShow):
    prefer_latest_item = False

    def play_next(self, present_time, media_class=None):
        # TODO there is a problem with the first play (is this still an issue?)
        # increment (advance to current) before checking if finished
        if media_class == "visual":
            if self.playlist.advance_to_current(
                present_time - self.start_time(), "image"
            ):
                self.play_current(present_time)
        elif media_class == "audio":
            if self.playlist.advance_to_current(
                present_time - self.start_time(), "audio"
            ):
                self.play_current(present_time)
        else:
            if self.playlist.advance_to_current(present_time - self.start_time()):
                self.play_current(present_time)

        if self.is_paused() or self.playlist.is_finished():
            self.ctrl.stop_requests()
            self.ctrl.add_request(
                media_type="break",
                end_time=self.end_time(),
                title="show finished break",
            )
            self.next_media_update = self.end_time()
            return False


class ObScheduler:
    def __init__(self, first_sync=False):
        self.lock = threading.Lock()
        self.first_sync = first_sync

        self.showfade_ctrl = obplayer.Player.create_controller(
            "showfade", priority=50, default_play_mode="overlap", allow_overlay=True
        )

        self.voicetrack_ctrl = obplayer.Player.create_controller(
            "voicetrack", priority=50, default_play_mode="overlap", allow_overlay=True
        )

        self.ctrl = obplayer.Player.create_controller(
            "scheduler", priority=50, default_play_mode="overlap", allow_overlay=False
        )

        self.ctrl.set_request_callback(self.do_player_request)
        self.ctrl.set_update_callback(self.do_player_update)

        self.voicetrack_ctrl.set_request_callback(self.do_voicetrack_request)
        self.voicetrack_ctrl.set_update_callback(self.do_voicetrack_update)

        self.showfade_duration = float(obplayer.Config.setting("fade_duration", 5))
        self.showfade_ctrl.set_request_callback(self.do_showfade_request)
        self.showfade_ctrl.set_update_callback(self.do_showfade_update)

        self.present_show = None
        self.next_show_update = 0

    def do_showfade_request(self, ctrl, present_time, media_class):
        self.do_showfade_update(ctrl, present_time)

    def do_showfade_update(self, ctrl, present_time):
        self.showfade_ctrl.set_next_update(present_time + 0.1)
        if (
            self.present_show
            and type(self.present_show) is ObShow
            and self.present_show.fadeout
        ):
            ending_in = self.present_show.end_time() - present_time
            fade_duration = self.showfade_duration

            # fade when needed, as long as we have at least 0.5s left to fade
            if ending_in <= fade_duration and ending_in > 0.5:
                obplayer.Player.outputs["mixer"].fade(
                    {
                        "element": "mixer-primary-volume",
                        "volume": 0.0,
                        "time": ending_in - 0.1,
                    }
                )

                self.present_show.fadeout = False

    def do_voicetrack_request(self, ctrl, present_time, media_class):
        self.do_voicetrack_update(ctrl, present_time)

    def do_voicetrack_update(self, ctrl, present_time):
        self.voicetrack_ctrl.set_next_update(present_time + 0.2)
        if self.present_show:
            self.present_show.play_current_voicetrack(present_time)

    def do_player_request(self, ctrl, present_time, media_class):
        self.check_show(present_time)

        if self.present_show is not None:
            self.present_show.play_next(present_time, media_class)

        self.set_next_update()

    def do_player_update(self, ctrl, present_time):
        self.check_show(present_time)

        if self.present_show and present_time > self.present_show.next_media_update:
            self.present_show.play_next(present_time)

        self.set_next_update()

    def set_next_update(self):
        if (
            self.present_show
            and self.present_show.next_media_update < self.next_show_update
        ):
            self.ctrl.set_next_update(self.present_show.next_media_update)
        else:
            self.ctrl.set_next_update(self.next_show_update)

    def check_show(self, present_time):
        if self.present_show is None or present_time > self.next_show_update:
            self.present_show = ObShow.find_show(present_time)
            next_show_times = obplayer.RemoteData.get_next_show_times(present_time)

            # no show, try again in 1min30secs.  (we add the 30 seconds to let the priority broadcaster update come through first, if applicable.
            if self.present_show is None:
                obplayer.Log.log("no show found.", "scheduler")

                # update now_playing data.
                obplayer.Sync.now_playing_update("", "", "", "", "")

                # if next show starting in less than 90s, we need to update sooner.
                self.next_show_update = present_time + 90
                if (
                    next_show_times
                    and self.next_show_update > next_show_times["start_time"]
                ):
                    self.next_show_update = next_show_times["start_time"]

            else:
                self.present_show.ctrl = self.ctrl
                self.present_show.voicetrack_ctrl = self.voicetrack_ctrl
                obplayer.Log.log(
                    "loading show " + str(self.present_show.show_id()), "scheduler"
                )

                # update now_playing data
                obplayer.Sync.now_playing_update(
                    self.present_show.show_id(),
                    self.present_show.end_time(),
                    "",
                    "",
                    self.present_show.name(),
                )
                # TODO get rid of this?? ^^^  why would i get rid of this, old me?

                # figure out show update time. (lower of next show start time, present show end time).
                self.next_show_update = self.present_show.end_time()
                if (
                    next_show_times
                    and self.next_show_update > next_show_times["start_time"]
                ):
                    self.next_show_update = next_show_times["start_time"]

                self.present_show.start_show(present_time)

                if self.present_show.playlist.is_finished():
                    if next_show_times != None:
                        # TODO next_show_start may not be defined
                        obplayer.Log.log(
                            "this show over. waiting for next show to start",
                            # "this show over. waiting for next show to start in "
                            # + str(self.next_show_start - present_time)
                            # + " seconds",
                            "scheduler",
                        )
                    else:
                        obplayer.Log.log(
                            "this show over. next show not found. will retry after next update."
                        )

    # update show update time after show sync. (if next show starting sooner than previously set, we need to update!)
    # this is already subject to the 'show lock' cutoff time.
    def update_show_update_time(self):
        next_show_times = obplayer.RemoteData.get_next_show_times(time.time())
        if next_show_times and next_show_times["start_time"] < self.next_show_update:
            self.next_show_update = next_show_times["start_time"]
            self.set_next_update()

    def get_show_name(self):
        if self.present_show == None:
            return "(no show playing)"
        return self.present_show.name()

    def get_show_info(self):
        if self.present_show == None:
            return None
        return self.present_show.show_info()

    def get_show_end(self):
        if self.present_show == None:
            return 0
        return self.present_show.end_time()

    def get_current_playlist(self):
        playlist = []
        if self.present_show != None:
            for track in self.present_show.get_playlist():
                data = {
                    "track_id": track["media_id"],
                    "artist": track["artist"],
                    "title": track["title"],
                    "duration": track["duration"],
                    "media_type": track["media_type"],
                }
                playlist.append(data)
        return playlist

    def get_current_groups(self):
        groups = []
        if self.present_show != None:
            for group in self.present_show.get_groups():
                group_items = []
                for group_item in group["items"]:
                    data = {
                        "id": group_item["id"],
                        "artist": group_item["artist"],
                        "title": group_item["title"],
                        "duration": group_item["duration"],
                        "media_type": group_item["media_type"],
                    }
                    group_items.append(data)
                groups.append({"name": group["name"], "items": group_items})
        return groups

    def playlist_seek(self, track_num, seek):
        if self.present_show == None:
            return False

        with self.lock:
            self.present_show.playlist_seek(track_num, seek)
        return True

    def play_group_item(self, group_num, group_item_num, seek):
        if self.present_show == None:
            return False

        with self.lock:
            self.present_show.play_group_item(group_num, group_item_num, seek)
        return True

    def set_stop_after(self, enable):
        if not isinstance(self.present_show, ObLiveAssistShow):
            return False

        with self.lock:
            self.present_show.stop_after = bool(enable)
            # the playing track may already have its crossfade set up
            obplayer.Player.set_controller_transition(self.ctrl, not enable)
        return True

    def unpause_show(self):
        if self.present_show == None:
            return False

        with self.lock:
            self.present_show.unpause()
        return True

    def pause_show(self, syncing=False):
        if self.present_show == None:
            return False

        with self.lock:
            self.present_show.pause(syncing)
        return True

    def next_track(self):
        if self.present_show == None:
            return False

        with self.lock:
            self.present_show.next()
        return True

    def previous_track(self):
        if self.present_show == None:
            return False

        with self.lock:
            self.present_show.previous()
        return True

    def find_group_item_pos(self, group_id):
        if self.present_show == None:
            return False

        groups = self.present_show.get_groups()
        for i in range(0, len(groups)):
            group_items = groups[i]["items"]
            for j in range(0, len(group_items)):
                if group_items[j]["id"] == group_id:
                    return (i, j)
        return (0, 0)

    def get_now_playing(self):
        data = {}

        player = self.ctrl.player
        requests = player.get_requests()

        request = None
        for key in requests.keys():
            if not request or requests[key]["priority"] > request["priority"]:
                request = requests[key]

        if not request:
            data["status"] = "override"
            data["artist"] = ""
            data["title"] = ""
            data["duration"] = 0
            data["position"] = 0
            data["track"] = -1
            return data

        if request["controller"] != self.ctrl:
            data["status"] = "override"
        elif self.ctrl.request_is_playing():
            data["status"] = "playing"
        else:
            data["status"] = "stopped"

        data["artist"] = request["artist"]
        data["title"] = request["title"]
        data["duration"] = request["duration"]
        data["position"] = time.time() - request["start_time"]

        # paused: report the paused track where it stopped, not the "show paused break" holding
        # the output (its position counts up from the pause, against a show-length duration)
        show = self.present_show
        if (
            show is not None
            and show.paused
            and show.now_playing is not None
            and request["controller"] == self.ctrl
            and request["media_type"] == "break"
            and 0 <= show.pause_position <= float(show.now_playing["duration"])
        ):
            data["artist"] = show.now_playing["artist"]
            data["title"] = show.now_playing["title"]
            data["duration"] = float(show.now_playing["duration"])
            data["position"] = show.pause_position

        if self.present_show != None and self.present_show.now_playing != None:
            now_playing = self.present_show.now_playing
            if "group_id" in now_playing:
                data["mode"] = "group"
                (data["group_num"], data["group_item_num"]) = self.find_group_item_pos(
                    now_playing["id"]
                )
            else:
                data["mode"] = "playlist"
                data["track"] = self.present_show.playlist.current_pos()

        if self.present_show != None:
            data["show_type"] = self.present_show.show_data["type"]
            data["stop_after"] = self.present_show.stop_after

        return data

    def get_audio_levels(self):
        return self.ctrl.player.get_audio_levels()
