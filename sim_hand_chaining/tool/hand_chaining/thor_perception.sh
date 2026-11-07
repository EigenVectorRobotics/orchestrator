#!/usr/bin/env bash
# Thor's button perception for the FVD sim: the Cosmos daemon and GroundBoxes, seed_bbox and track_3d (EdgeTAM), as
# deployed in ~/eigen_vector_ws (nothing is copied to Thor). ROS domain 73, localhost discovery plus vector-pc as the
# static peer, so it sees only the sim. In tmux -L fvd_sim on Thor; stop puts the edgellm container back as it was.
# Usage (from the Mac): ssh vector@vector-thor 'bash -s -- start|stop|status' < thor_perception.sh

PEER=100.118.78.37  # vector-pc on tailscale
WS=$HOME/eigen_vector_ws
DAEMON=$WS/install_cosmos/vlm/share/vlm/tools/edgellm_daemon.sh
# install/ has track_anything; install_cosmos/ the vlm thor branch (Cosmos), whose layout the old install/vlm misses
ENV="cd $WS && source env.sh && source install_cosmos/setup.bash && export RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
ROS_DOMAIN_ID=73 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST ROS_STATIC_PEERS=$PEER"

case "$1" in
start)
    [[ $(docker inspect -f '{{.State.Running}}' edgellm) == true ]] || touch /tmp/fvd_sim_started_edgellm
    # The resident Cosmos daemon (started if needed), then the GroundBoxes service
    tmux -L fvd_sim new-session -d -s perception -n ground "bash -c '$ENV && ros2 launch vlm cosmos_thor.launch.py 2>&1 | tee /tmp/fvd_sim_ground.log'"
    # Compressed frames: raw 640x480 frames (1.5 MB a pair) do not get through DDS over tailscale
    tmux -L fvd_sim new-window -t perception -n seed "bash -c '$ENV && ros2 launch vlm seed_bbox.launch.py use_compressed:=true \
2>&1 | tee /tmp/fvd_sim_seed.log'"
    # seed_bbox takes the queries: track_3d listens for text on an unused topic and starts from the seed box
    tmux -L fvd_sim new-window -t perception -n track "bash -c '$ENV && ros2 launch track_anything track_3d.launch.py \
keyboard_topic:=/track_3d/unused_query use_compressed:=true 2>&1 | tee /tmp/fvd_sim_track.log'"
    echo "started"
    ;;
stop)
    tmux -L fvd_sim kill-server 2>/dev/null
    $DAEMON stop
    if [[ -e /tmp/fvd_sim_started_edgellm ]]; then
        docker stop edgellm > /dev/null && rm /tmp/fvd_sim_started_edgellm && echo "edgellm container stopped"
    fi
    ;;
status)
    tmux -L fvd_sim ls 2>&1
    $DAEMON status
    tail -n 3 /tmp/fvd_sim_ground.log /tmp/fvd_sim_seed.log /tmp/fvd_sim_track.log 2>/dev/null
    ;;
esac
