#!/bin/bash

if [ -d "${HOME}/nrc_pkg_bk" ]; then
 echo "Remove backup folder"
 rm -rf ${HOME}/nrc_pkg_bk
fi
if [ -d "${HOME}/nrc_pkg" ]; then
 echo "Backup previous package"
 mv ${HOME}/nrc_pkg ${HOME}/nrc_pkg_bk
fi
sleep 1

echo "Copy new package"
echo "apply nrc_pkg "
cp -r ./nrc_pkg/  ${HOME}/nrc_pkg/

echo "Change mode"
cd ${HOME}/nrc_pkg
sudo chmod -R 755 *
sudo chmod -R 777 ${HOME}/nrc_pkg/script/*
sudo chmod -R 777 ${HOME}/nrc_pkg/sw/firmware/copy
sleep 1

echo "Done"
