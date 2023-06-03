"""
rtl_sdr Audio Splitter

Splits the output of rtl_sdr commands into multiple outputs

Outputs
-------
There are two types of outputs. 
    - Streams
      Output is copied to the stream immediately (e.g., stdout)
    - Buffered Streams
      These can be dynamically enabled and disabled, via 'recording' and
      'monitor' mode. For recording, these use a ring buffer to store prior
      samples, so that the output prior to the trigger can be saved.

Since these buffered streams can start at any time, care must be taken around
the data framing. For example in an audio stream, we don't want to send the
second byte of a 16bit sample first, else the entire stream will be
unsynchronized.

A very basic framing implementation is implemented in the buffer. It is
susceptible to change in formats so use caution.

Recording Mode
--------------

The mode can be set using Signals

- SIGUSR1 - Start Recording
- SIGUSR2 - Stop Recording

For UDP control, send a byte with the desired mode bits set:

- 0x01 Record
- 0x10 Monitor

- 0x00 Stop Record and Monitor
- 0x11 Activate Record and Monitor


Example from Bash to enable Monitoring:

    printf '\x10' | nc -u -q 1 0.0.0.0 12345 
"""

# TODO and Ideas
# - Max record time
# - UDP command that takes a 256 byte msg string for later reporting
# - Send message with alert msg upon file recording finish
#   - or send the whole file, e.g., email it
#   - UDP commands that differentiate whether or not to send the alert after,
#     e.g., only record but don't send tests

import argparse
from collections import deque
import datetime
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import List
from wave import Wave_write


class AudioSplitter():
    """
    Main class that will receive input and split to outputs
    """

    def __init__(self, args):

        self.args = args

        # Circular Audio Buffer
        # each item is a bytearray of a complete frame of audio
        # (nBbytes * nChannels)
        self.AUDIO_BUFFER = deque(maxlen=args.sample_rate * args.buffer_size)

        # used to store partial audio frames
        self.SAMPLE_BUFFER = bytearray()

        self.recording = False
        self.monitoring = False

        self.streamOutputs = set()  # Send immediately
        self.recordOutputs = set()  # Send only on record
        self.monitorOutputs = set()

        self.bufferControlLock = threading.Lock()


    def addOutput(self, output, stream=False, record=False, monitor=False):
        if stream:
            self.streamOutputs.add(output)
        if record:
            self.recordOutputs.add(output)
        if monitor:
            self.monitorOutputs.add(output)

    def setMode(self, record=None, monitor=None):

        with self.bufferControlLock:
            if record is not None:
                self.recording = record
            if monitor is not None:
                self.monitoring = monitor
            
            # Monitors to stop
            allOutputs = self.recordOutputs | self.monitorOutputs
            activeOutputs = set()
            if self.recording:
                activeOutputs |= self.recordOutputs
            if self.monitoring:
                activeOutputs |= self.monitorOutputs

            for output in (allOutputs - activeOutputs):
                output.stop()

            # Start monitors instantly
            if self.monitoring:
                for output in self.monitorOutputs:
                    if not output.isActive():
                        output.start()

            # If recording, start and add buffer
            if self.recording:
                startedOutputs = set()
                for output in self.recordOutputs:
                    if not output.isActive():
                        output.start()
                        startedOutputs.add(output)
                if startedOutputs:
                    for sample in list(self.AUDIO_BUFFER):
                        for output in startedOutputs:
                            output.write(sample)
    
                    self.AUDIO_BUFFER.clear()


    def process_input(self, i_bytes: bytearray):
        """
        Either add to the buffer, or write to file if recording is active
        """

        with self.bufferControlLock:

            # Send streaming immediately
            for output in self.streamOutputs:
                output.write(i_bytes)
    
            # This bundle of joy is my naive attempt at ensuring synchronized
            # framing through the buffer
    
            def _outputFrame(b):
                if self.monitoring:
                    for output in self.monitorOutputs:
                        output.write(b)

                if self.recording:
                    recordOutputs = self.recordOutputs
                    if self.monitoring:
                        recordOutputs -= self.monitorOutputs
                    for output in recordOutputs:
                        output.write(b)
                else:
                    self.AUDIO_BUFFER.append(b)
    
            x = 0
            input_len = len(i_bytes)
            if self.SAMPLE_BUFFER:
                while len(self.SAMPLE_BUFFER) < self.args.sample_bytes and x < input_len:
                    self.SAMPLE_BUFFER.append(i_bytes[x])
                    x += 1
                if len(self.SAMPLE_BUFFER) == self.args.sample_bytes:
                    _outputFrame(self.SAMPLE_BUFFER)
                    self.SAMPLE_BUFFER = bytearray()
            while x < input_len:
                if (input_len - x) >= self.args.sample_bytes:
                    _outputFrame(i_bytes[x:x + self.args.sample_bytes])
                    x += self.args.sample_bytes
                else:
                    self.SAMPLE_BUFFER = i_bytes[x:]
                    break


class OutputBase():
    def __init__(self, args):
        self.active = False
        self.args = args

    def write(self, b: bytearray):
        pass

    def start(self):
        """
        May be called multiple times, ensure to check if started prior
        """
        self.active = True

    def stop(self):
        """
        May be called multiple times, ensure to check if started prior
        """
        self.active = False

    def isActive(self):
        return self.active

class StdoutOuput(OutputBase):

    def write(self, b: bytearray):
        sys.stdout.buffer.write(b)


class BufferedSubProcessOutputBase(OutputBase):

    def __init__(self, args):
        super().__init__(args)
        self.process = None

    def write(self, b):
        if self.process:
            self.process.stdin.write(b)

    def stop(self):
        if self.process:
            self.process.terminate()
        self.process = None

    def isActive(self):
        return self.process is not None


class BufferedWavOutput(BufferedSubProcessOutputBase):

    def start(self):
        if self.process:
            return
        filename = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".wav"
        filepath = os.path.join(self.args.recording_dir, filename)
        self.process = subprocess.Popen(
            f'sox -t raw -b {self.args.sample_bytes * 8} -e signed -r {self.args.sample_rate} -c1 - "{filepath}"',
            shell=True,
            stdin=subprocess.PIPE,
        )


class BufferedMP3Output(BufferedSubProcessOutputBase):

    def start(self):
        if self.process:
            return
        filename = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".mp3"
        filepath = os.path.join(self.args.recording_dir, filename)
        self.process = subprocess.Popen(
            f'sox -t raw -b {self.args.sample_bytes * 8} -e signed -r {self.args.sample_rate} -c1 - "{filepath}"',
            shell=True,
            stdin=subprocess.PIPE,
        )


class BufferedAudioOutput(BufferedSubProcessOutputBase):

    def start(self):
        if self.process:
            return
        self.process = subprocess.Popen(
            f"play -t raw -r {self.args.sample_rate} -es -b {self.args.sample_bytes * 8} -c 1 -V1 - | aplay -r {self.args.sample_rate} -f S{self.args.sample_bytes * 8}_LE -t raw -c 1 > /dev/null",
            shell=True,
            stdin=subprocess.PIPE,
        )


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Splits an audio stream on stdin to dynamic destinations",
        prog="cliAudioTee",
    )

    ###
    # UDP Control Port
    parser.add_argument(
        '--udp_port',
        default=12345,
        help='UDP port for monitor/record control'
    )
    parser.add_argument(
        '--udp_host',
        default="0.0.0.0",
        help='UDP bind host for monitor/record control'
    )

    ###
    # Audio Format

    parser.add_argument(
        '--sample_rate',
        default=22050,
        help='Audio Sample Rate (hz)',
    )

    parser.add_argument(
        '--sample_bytes',
        default=2,
        help='Audio Sample Byte-depth (bit depth/8)',
    )

    ###
    # Recording

    parser.add_argument(
        '--recording_dir',
        default="Recordings",
        help='Directory for recordings',
    )

    parser.add_argument(
        '--buffer_size',
        default=5,
        help='Audio Buffer Size for pre-recording (seconds)',
    )

    args, unknown = parser.parse_known_args()
    return args


args = parse_arguments()


audioSplitter = AudioSplitter(args)
audioSplitter.addOutput(StdoutOuput(args), stream=True)
audioSplitter.addOutput(BufferedWavOutput(args), record=True)
audioSplitter.addOutput(BufferedMP3Output(args), record=True)
audioSplitter.addOutput(BufferedAudioOutput(args), record=True, monitor=True)

shutdownEvent = threading.Event()

def shutdown():
    # only call from the main thread
    shutdownEvent.set()
    time.sleep(0.5)
    sys.exit()


def signal_handler(signum, frame):
    if signum == signal.SIGUSR1:
        audioSplitter.setMode(record=True)
    elif signum == signal.SIGUSR2:
        audioSplitter.setMode(record=False, monitor=False)
    elif signum == signal.SIGHUP:
        shutdown()
    elif signum == signal.SIGINT:
        shutdown()


def input_thread():
    while True:
        i_bytes = bytes(sys.stdin.buffer.read(128))
        if shutdownEvent.is_set():
            return
        if i_bytes:
            audioSplitter.process_input(i_bytes)
        else:
            sys.stderr.write("Lost Input - Terminating\n")
            shutdownEvent.set()
            return

def udp_control_thread(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind( (host, port) )
    sock.settimeout(0.01)

    while True:
        data = None
        try:
            data = sock.recv(1)
        except TimeoutError:
            pass
        if shutdownEvent.is_set():
            return
        if data:
            audioSplitter.setMode(
                record=data[0] & 0x01,
                monitor=data[0] & 0x10,
            )


i_thread = threading.Thread(target=input_thread)
i_thread.daemon = True
i_thread.start()

control_thread = threading.Thread(
    target=udp_control_thread,
    args=(args.udp_host, args.udp_port),
)
control_thread.daemon = True
control_thread.start()

signal.signal(signal.SIGUSR1, signal_handler)
signal.signal(signal.SIGUSR2, signal_handler)
signal.signal(signal.SIGHUP, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


while True:
    time.sleep(.1)
    if shutdownEvent.is_set():
        shutdown()


