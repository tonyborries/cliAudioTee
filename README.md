cliAudioTee
===========

This is intended as a connector for CLI scripts to sit between RTL-SDR source
programs and the processing programs.

The initial use case is to dynamically enable recording of the audio stream when a trigger is detected in the processing. Since processing will lag the signal, it includes a ring buffer to include prior audio in the recording.


Limitations
-----------

This is a hacky implementation at best, and is likely not suitable for out-of-the-box use. I'm publishing this after several incomplete iterations, and by no means is this a polished product.

This is a quick, dirty, and inefficient implementation, and while it shows negligible CPU load on a PC, may have issues running on a limited device such as a Raspberry PI.

I have noticed issues of subprocesses not exiting, possibly keeping resources locked.

The end of message trigger often gets missed in my dsame testing below.

Future Work / Ideas
-------------------

I have no specific plans to further this script, I'm releasing it only as a possible starting point for others.

- Ideally, the ring buffer would be redone in C and include proper audio framing
- Outputs could include Icecast streams for remote monitoring
- Currently the input is just STDIN, but could include files or icecast streams.
- Implement a Max record time so a missed End Of Message doesn't record indefinitely.

demo dsame3 integration
-----------------------

modified dsame3 to send the UDP command on 'record'

```
@@ -57,6 +57,13 @@ def callback(indata, data, frames, status):
 
 
 def set_is_recording(data):
+    import socket
+    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
+    print(f"Recording: {data}")
+    s.sendto(
+        b'\x01' if data else b'\x00',
+        ('0.0.0.0', 12345)
+    )
     global is_recording
     is_recording = data
```

dsame source script:

```
echo INPUT: rtl_fm Device 1>&2
PPM=0
FREQ=162.525M
GAIN=42
until rtl_fm -f ${FREQ} -M fm -s 22050 -E dc -p ${PPM}  - | python3 cliAudioTee.py | multimon-ng -t raw -a EAS /dev/stdin; do
    echo Restarting... >&2
    sleep 2
done
```

