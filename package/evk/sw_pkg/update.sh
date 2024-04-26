#!/bin/bash

if [ -d "/home/${USER}/nrc_pkg_bk" ]; then
 echo "Remove backup folder"
 rm -rf /home/${USER}/nrc_pkg_bk
fi
if [ -d "/home/${USER}/nrc_pkg" ]; then
 echo "Backup previous package"
 mv /home/${USER}/nrc_pkg /home/${USER}/nrc_pkg_bk
fi
sleep 1

echo "Copy new package"
echo "apply nrc_pkg "
cp -r ./nrc_pkg/  /home/${USER}/nrc_pkg/

echo "Change mode"
cd /home/${USER}/nrc_pkg
sudo chmod -R 755 *
sudo chmod -R 777 /home/${USER}/nrc_pkg/script/*
sudo chmod -R 777 /home/${USER}/nrc_pkg/sw/firmware/copy
sleep 1

echo "Done"
