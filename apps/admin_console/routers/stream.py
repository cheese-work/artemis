# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Device Screen Live Streaming Router.

Exposes real-time screen streaming endpoints for the Web UI.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from apps.admin_console.core.device_ownership import may_use_device
from apps.admin_console.core.ownership import OwnerScope, actor_scope, require_actor

try:
    from admin_console.services.device_stream_service import device_stream_service
except ImportError:
    from apps.admin_console.services.device_stream_service import device_stream_service

router = APIRouter(tags=["stream"])


@router.get("/api/stream/device-live")
async def stream_device_live(actor: OwnerScope = Depends(actor_scope)):
    """Streams live device screen frames as multipart MJPEG.

    A signed-in QA only ever receives frames from their own or a shared phone.
    """
    scope = require_actor(actor)
    scoped = scope.enforced and not scope.admin
    return StreamingResponse(
        device_stream_service.mjpeg_frame_generator(
            (lambda serial: may_use_device(scope, serial)) if scoped else None
        ),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "Connection": "keep-alive",
        },
    )


@router.get("/api/stream/device-state")
async def get_device_stream_state(actor: OwnerScope = Depends(actor_scope)):
    """Returns whether an ADB device is connected and live streaming is available."""
    scope = require_actor(actor)
    serial = await device_stream_service.get_device_serial()
    if serial and not may_use_device(scope, serial):
        serial = None  # someone else's private phone is not reported at all
    return JSONResponse(
        {
            "connected": serial is not None,
            "serial": serial,
            "live_stream_url": "/api/stream/device-live" if serial else None,
        }
    )
