# Timelapse plugin
#
# Copyright (C) 2020 Christoph Frei <fryakatkop@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations
import logging
import os
import glob
import re
import shutil
from datetime import datetime
from tornado.ioloop import IOLoop
from zipfile import ZipFile

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Dict,
    Any
)
if TYPE_CHECKING:
    from confighelper import ConfigHelper
    from websockets import WebRequest
    from . import shell_command
    SCMDComp = shell_command.ShellCommandFactory


class Timelapse:

    def __init__(self, config: ConfigHelper) -> None:
        # setup vars
        self.renderisrunning = False
        self.saveisrunning = False
        self.takingframe = False
        self.framecount = 0
        self.lastframefile = ""
        self.lastrenderprogress = 0

        # get config
        self.enabled = config.getboolean("enabled", True)
        self.autorender = config.getboolean("autorender", True)
        self.crf = config.getint("constant_rate_factor", 23)
        self.framerate = config.getint("output_framerate", 30)
        self.variablefps = config.getboolean("variablefps", False)
        self.targetlength = config.getint("targetlength", 60)
        self.min_framerate = config.getint("min_framerate", 5)
        self.timeformatcode = config.get("time_format_code", "%Y%m%d_%H%M")
        self.snapshoturl = config.get(
            "snapshoturl", "http://localhost:8080/?action=snapshot")
        self.pixelformat = config.get("pixelformat", "yuv420p")
        self.extraoutputparams = config.get("extraoutputparams", "")
        out_dir_cfg = config.get("output_path", "~/timelapse/")
        temp_dir_cfg = config.get("frame_path", "/tmp/timelapse/")
        self.ffmpeg_binary_path = config.get(
            "ffmpeg_binary_path", "/usr/bin/ffmpeg")
        self.previewImage = config.getboolean("previewImage", True)
        self.preserveFrames = config.getboolean("preserveFrames", False)

        # check if ffmpeg is installed
        self.ffmpeg_installed = os.path.isfile(self.ffmpeg_binary_path)
        if not self.ffmpeg_installed:
            self.autorender = False
            logging.info(f"timelapse: {self.ffmpeg_binary_path} \
                        not found please install to use render functionality")

        # setup directories
        # remove trailing "/"
        out_dir_cfg = os.path.join(out_dir_cfg, '')
        temp_dir_cfg = os.path.join(temp_dir_cfg, '')
        # evaluate and expand "~"
        self.out_dir = os.path.expanduser(out_dir_cfg)
        self.temp_dir = os.path.expanduser(temp_dir_cfg)
        # create directories if they doesn't exist
        os.makedirs(self.temp_dir, exist_ok=True)
        os.makedirs(self.out_dir, exist_ok=True)

        # setup eventhandlers and endpoints
        self.server = config.get_server()
        file_manager = self.server.lookup_component("file_manager")
        file_manager.register_directory("timelapse", self.out_dir)
        file_manager.register_directory("timelapse_frames", self.temp_dir)
        self.server.register_notification("timelapse:timelapse_event")
        self.server.register_event_handler(
            "server:gcode_response", self.handle_status_update)
        self.server.register_remote_method(
            "timelapse_newframe", self.call_timelapse_newframe)
        self.server.register_remote_method(
            "timelapse_saveFrames", self.call_timelapse_saveFrames)
        self.server.register_remote_method(
            "timelapse_render", self.call_timelapse_render)
        self.server.register_endpoint(
            "/machine/timelapse/render", ['POST'], self.timelapse_render)
        self.server.register_endpoint(
            "/machine/timelapse/settings", ['GET', 'POST'],
            self.webrequest_timelapse_settings)
        self.server.register_endpoint(
            "/machine/timelapse/lastframeinfo", ['GET'],
            self.webrequest_timelapse_lastframeinfo)

    async def webrequest_timelapse_lastframeinfo(self,
                                                 webrequest: WebRequest
                                                 ) -> Dict[str, Any]:
        return {
            'framecount': self.framecount,
            'lastframefile': self.lastframefile
        }

    async def webrequest_timelapse_settings(self,
                                            webrequest: WebRequest
                                            ) -> Dict[str, Any]:
        action = webrequest.get_action()
        if action == 'POST':
            args = webrequest.get_args()
            logging.debug("webreq_args: " + str(args))
            for arg in args:
                val = args.get(arg)
                if arg == "enabled":
                    self.enabled = webrequest.get_boolean(arg)
                if arg == "autorender" and self.ffmpeg_installed:
                    self.autorender = webrequest.get_boolean(arg)
                if arg == "constant_rate_factor":
                    self.crf = webrequest.get_int(arg)
                if arg == "output_framerate":
                    self.framerate = webrequest.get_int(arg)
                if arg == "pixelformat":
                    self.pixelformat = webrequest.get(arg)
                if arg == "extraoutputparams":
                    self.extraoutputparams = webrequest.get(arg)
                if arg == "variablefps":
                    self.variablefps = webrequest.get_boolean(arg)
                if arg == "targetlength":
                    self.targetlength = webrequest.get_int(arg)
                if arg == "min_framerate":
                    self.min_framerate = webrequest.get_int(arg)
        return {
            'enabled': self.enabled,
            'autorender': self.autorender,
            'constant_rate_factor': self.crf,
            'output_framerate': self.framerate,
            'pixelformat': self.pixelformat,
            'extraoutputparams': self.extraoutputparams,
            'variablefps': self.variablefps,
            'targetlength': self.targetlength,
            'min_framerate': self.min_framerate
        }

    def call_timelapse_newframe(self) -> None:
        if self.enabled:
            ioloop = IOLoop.current()
            ioloop.spawn_callback(self.timelapse_newframe)
        else:
            logging.debug("NEW_FRAME macro ignored timelapse is disabled")

    async def timelapse_newframe(self) -> None:
        if not self.takingframe:
            self.takingframe = True
            self.framecount += 1
            framefile = "frame" + str(self.framecount).zfill(6) + ".jpg"
            cmd = "wget " + self.snapshoturl + " -O " \
                  + self.temp_dir + framefile
            self.lastframefile = framefile
            logging.debug(f"cmd: {cmd}")

            shell_cmd: SCMDComp = self.server.lookup_component('shell_command')
            scmd = shell_cmd.build_shell_command(cmd, None)
            try:
                cmdstatus = await scmd.run(timeout=2., verbose=False)
            except Exception:
                logging.exception(f"Error running cmd '{cmd}'")

            result = {'action': 'newframe'}
            if cmdstatus:
                result.update({
                    'frame': str(self.framecount),
                    'framefile': framefile,
                    'status': 'success'
                })
            else:
                logging.info(f"getting newframe failed: {cmd}")
                self.framecount -= 1
                result.update({'status': 'error'})

            self.notify_timelapse_event(result)
            self.takingframe = False

    async def webrequest_timelapse_render(self, webrequest: WebRequest) -> str:
        ioloop = IOLoop.current()
        ioloop.spawn_callback(self.timelapse_render)
        return "ok"

    def handle_status_update(self, status: str) -> None:
        if status == "File selected":
            # print_started
            self.timelapse_cleanup()
        elif status == "Done printing file":
            # print_done
            if self.enabled and self.preserveFrames:
                ioloop = IOLoop.current()
                ioloop.spawn_callback(self.timelapse_saveFrames)
            if self.enabled and self.autorender:
                ioloop = IOLoop.current()
                ioloop.spawn_callback(self.timelapse_render)

    def timelapse_cleanup(self) -> None:
        logging.debug("timelapse_cleanup")
        filelist = glob.glob(self.temp_dir + "frame*.jpg")
        if filelist:
            for filepath in filelist:
                os.remove(filepath)
        self.framecount = 0
        self.lastframefile = ""

    def call_timelapse_saveFrames(self) -> None:
        ioloop = IOLoop.current()
        ioloop.spawn_callback(self.timelapse_saveFrames)

    async def timelapse_saveFrames(self) -> None:
        filelist = sorted(glob.glob(self.temp_dir + "frame*.jpg"))
        self.framecount = len(filelist)

        if not filelist:
            msg = "no frames to save, skip"
            status = "skipped"
        elif self.saveisrunning:
            msg = "saving frames already"
            status = "running"
        else:
            self.saveisrunning = True

            # get printed filename
            klippy_apis = self.server.lookup_component("klippy_apis")
            kresult = await klippy_apis.query_objects({'print_stats': None})
            pstats = kresult.get("print_stats", {})
            gcodefile = pstats.get("filename", "").split("/")[-1]

            # prepare output filename
            now = datetime.now()
            date_time = now.strftime(self.timeformatcode)
            outfile = f"timelapse_{gcodefile}_{date_time}"

            zipObj = ZipFile(self.out_dir + outfile + "_frames.zip", "w")

            for frame in filelist:
                zipObj.write(frame, frame.split("/")[-1])

            logging.info(f"saved frames: {outfile}_frames.zip")

            self.saveisrunning = False

    def call_timelapse_render(self) -> None:
        ioloop = IOLoop.current()
        ioloop.spawn_callback(self.timelapse_render)

    async def timelapse_render(self, webrequest=None:
        filelist = sorted(glob.glob(self.temp_dir + "frame*.jpg"))
        self.framecount = len(filelist)
        result = {'action': 'render'}

        if not filelist:
            msg = "no frames to render, skip"
            status = "skipped"
        elif self.renderisrunning:
            msg = "render is already running"
            status = "running"
        elif not self.ffmpeg_installed:
            msg = f"{self.ffmpeg_binary_path} not found, please install ffmpeg"
            status = "error"
            # cmd = outfile = None
            logging.info(f"timelapse: {msg}")
        else:
            self.renderisrunning = True

            # get printed filename
            klippy_apis = self.server.lookup_component("klippy_apis")
            kresult = await klippy_apis.query_objects({'print_stats': None})
            pstats = kresult.get("print_stats", {})
            gcodefile = pstats.get("filename", "").split("/")[-1]

            # prepare output filename
            now = datetime.now()
            date_time = now.strftime(self.timeformatcode)
            inputfiles = self.temp_dir + "frame%6d.jpg"
            outfile = f"timelapse_{gcodefile}_{date_time}"

            # variable framerate
            if self.variablefps:
                fps = int(self.framecount / self.targetlength)
                fps = max(min(fps, self.framerate), self.min_framerate)
            else:
                fps = self.framerate

            # build shell command
            cmd = self.ffmpeg_binary_path \
                + " -r " + str(fps) \
                + " -i '" + inputfiles + "'" \
                + " -threads 2 -g 5" \
                + " -crf " + str(self.crf) \
                + " -vcodec libx264" \
                + " -pix_fmt " + self.pixelformat \
                + " -an" \
                + " " + self.extraoutputparams \
                + " '" + self.out_dir + outfile + ".mp4' -y"

            # log and notify ws
            logging.debug(f"start FFMPEG: {cmd}")
            result.update({
                'status': 'started',
                'framecount': str(self.framecount),
                'settings': {
                    'framerate': self.framerate,
                    'crf': self.crf,
                    'pixelformat': self.pixelformat
                }
            })

            # run the command
            shell_cmd: SCMDComp = self.server.lookup_component('shell_command')
            self.notify_timelapse_event(result)
            scmd = shell_cmd.build_shell_command(cmd, self.ffmpeg_cb)
            try:
                cmdstatus = await scmd.run(verbose=True,
                                           log_complete=False,
                                           timeout=9999999999,
                                           )
            except Exception:
                logging.exception(f"Error running cmd '{cmd}'")

            # check success
            if cmdstatus:
                status = "success"
                msg = f"Rendering Video successful: {outfile}.mp4"
                result.update({
                    'filename': f"{outfile}.mp4",
                    'printfile': gcodefile
                })
                result.pop("framecount")
                result.pop("settings")

                # copy image preview
                if self.previewImage:
                    previewfile = f"{outfile}.jpg"
                    previewSrc = filelist[-1:][0]
                    # logging.debug(f"deadbeef lastframe: {previewSrc}")
                    try:
                        shutil.copy(previewSrc, self.out_dir + previewfile)
                    except OSError as err:
                        logging.info(f"copying preview image failed: {err}")
                    else:
                        result.update({
                            'previewImage': previewfile
                        })
            else:
                status = "error"
                msg = f"Rendering Video failed"
                result.update({
                    'cmd': cmd
                })

            self.renderisrunning = False

        # log and notify ws
        logging.info(msg)
        result.update({
            'status': status,
            'msg': msg
        })
        self.notify_timelapse_event(result)

        return result

    def ffmpeg_cb(self, response):
        # logging.debug(f"ffmpeg_cb: {response}")
        lastcmdreponse = response.decode("utf-8")
        try:
            frame = re.search(
                r'(?<=frame=)*(\d+)(?=.+fps)', lastcmdreponse
            ).group()
        except AttributeError:
            return
        percent = int(frame) / self.framecount * 100
        if percent > 100:
            percent = 100

        if self.lastrenderprogress != int(percent):
            self.lastrenderprogress = int(percent)
            # logging.debug(f"ffmpeg Progress: {self.lastrenderprogress}% ")
            result = {
                'action': 'render',
                'status': 'running',
                'progress': self.lastrenderprogress
            }
            self.notify_timelapse_event(result)

    def notify_timelapse_event(self, result: Dict[str, Any]) -> None:
        logging.debug(f"notify_timelapse_event: {result}")
        self.server.send_event("timelapse:timelapse_event", result)


def load_component(config: ConfigHelper) -> Timelapse:
    return Timelapse(config)
