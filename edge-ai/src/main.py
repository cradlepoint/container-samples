"""Application entry point for Edge AI Person Detection.

Initializes all components, starts processing and web server threads,
and handles graceful shutdown on SIGTERM/SIGINT.

Requirements: 1.1, 1.6, 2.7, 7.5, 7.7, 8.2, 10.1, 10.4
"""
import sys
import os
import signal
import threading
import time

# Add parent directory to path so cp module is importable
_parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

# Add src directory to path for sibling module imports
_src_dir = os.path.dirname(os.path.abspath(__file__))
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

import cp

from config import ConfigLoader
from capture import RTSPCapture
from inference import InferenceEngine
from annotation import AnnotationRenderer
from processor import FrameProcessor
from web_server import WebServer


# Application version
APP_VERSION = "1.0.0"

# Model paths inside the container
MODEL_PATHS = {
    'ssd_mobilenet_v2': '/app/models/ssd_mobilenet_v2.tflite',
    'yolov5n': '/app/models/yolov5n_int8.tflite',
}


# Shutdown event shared across threads
_shutdown_event = threading.Event()


def _signal_handler(signum, frame):
    # type: (int, object) -> None
    """Handle SIGTERM/SIGINT for graceful shutdown.

    Sets the shutdown event so all threads can exit cleanly.

    Args:
        signum: Signal number received.
        frame: Current stack frame (unused).
    """
    sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    cp.log("Received {}, initiating graceful shutdown...".format(sig_name))
    _shutdown_event.set()


def main():
    # type: () -> None
    """Application entry point running inside the container.

    Orchestrates initialization of all components, starts threads,
    and waits for shutdown signal.
    """
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Load and validate configuration
    config_loader = ConfigLoader()
    config = config_loader.load_config()

    # Validate system resources
    cpu_count = os.cpu_count() or 2
    cp.log("  CPUs available: {}".format(cpu_count))

    # Resolve model path from config
    model_path = MODEL_PATHS.get(config.model_name, MODEL_PATHS['ssd_mobilenet_v2'])

    # Log startup info (Requirement 10.1)
    cp.log("Edge AI Person Detection v{} starting".format(APP_VERSION))
    cp.log("  Model: {} ({})".format(config.model_name, os.path.basename(model_path)))
    cp.log("  RTSP URL: {}".format(config.rtsp_input_url if config.rtsp_input_url else "(not configured)"))
    cp.log("  Confidence threshold: {}".format(config.confidence_threshold))

    # Handle missing RTSP URL (Requirement 8.2)
    # Serve web UI with configuration message, don't start capture/processing
    if not config.rtsp_input_url:
        cp.log("WARNING: No RTSP URL configured. Starting web server only.")
        cp.log("Please configure an RTSP URL via the web interface.")

        # Start web server without a processor (serves placeholder/config page)
        web = WebServer(config.web_port, None)
        web.start()

        cp.log("Web server running on port {}. Waiting for configuration...".format(
            config.web_port))

        # Wait for shutdown signal
        while not _shutdown_event.is_set():
            _shutdown_event.wait(timeout=1.0)

        web.stop()
        cp.log("Edge AI shut down (no RTSP URL configured)")
        return

    # Initialize inference engine and load model (Requirement 2.7)
    engine = InferenceEngine(model_path, config.confidence_threshold, num_threads=cpu_count)
    if not engine.load_model():
        cp.log("ERROR: Failed to load model at {}. Exiting.".format(model_path))
        sys.exit(1)

    # Initialize RTSP capture
    capture = RTSPCapture(config.rtsp_input_url, config.target_fps)

    # Don't connect on startup — wait for user to press Play
    cp.log("RTSP capture initialized (not connected — press Play to start)")

    # Initialize annotation renderer and frame processor
    renderer = AnnotationRenderer()
    processor = FrameProcessor(capture, engine, renderer, config.target_fps)
    processor.skip_inference_frames = config.skip_inference_frames

    # Initialize web server with processor
    web = WebServer(config.web_port, processor, jpeg_quality=config.jpeg_quality)

    # Wire web server into processor for client-aware annotation skipping
    processor.web_server = web

    # Start processing thread
    processing_thread = threading.Thread(
        target=processor.process_loop,
        name="ProcessingThread"
    )
    processing_thread.daemon = True
    processing_thread.start()

    # Start web server (runs in its own daemon thread)
    web.start()

    cp.log("Edge AI fully started. Processing at {} FPS target.".format(
        config.target_fps))

    # Main thread: wait for shutdown signal
    while not _shutdown_event.is_set():
        _shutdown_event.wait(timeout=1.0)

    # Graceful shutdown
    cp.log("Shutting down Edge AI...")

    # Stop processor first (stops reading frames)
    processor.stop()

    # Stop capture (breaks reconnect loop if active)
    capture.stop()

    # Stop web server
    web.stop()

    # Wait for processing thread to finish
    processing_thread.join(timeout=5.0)

    cp.log("Edge AI shut down complete")


if __name__ == "__main__":
    main()
