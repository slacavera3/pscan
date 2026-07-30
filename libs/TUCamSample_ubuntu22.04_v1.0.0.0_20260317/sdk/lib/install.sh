#!/bin/bash

folder="/etc/tucam/"

if [ ! -d "$folder" ]; then
  mkdir "$folder"
fi

# copy the tucsen usb camera config file
cp tuusb.conf /etc/tucam
cp 50-tuusb.rules /etc/udev/rules.d

# copy the tucsen camera libraries
cp libTUCam.so /usr/lib
cp libTUCam.so.1 /usr/lib
cp libTUCam.so.1.0 /usr/lib
cp libTUCam.so.1.0.0 /usr/lib
cp libMultiFile.so /usr/lib
cp libMultiFile.so.1 /usr/lib
cp libMultiFile.so.1.0 /usr/lib
cp libMultiFile.so.1.0.0 /usr/lib
cp lib*_gcc48_v3_2.so /usr/lib
