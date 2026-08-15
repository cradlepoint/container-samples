/* Minimal slippy map: tile layer plus a canvas overlay.
 *
 * Written from scratch rather than vendoring a mapping library. The container
 * has to be self-contained (the router may have no internet access, so a CDN
 * script tag cannot be relied on), and bundling a minified third-party library
 * into a sample repo hides code the reader is meant to be able to follow.
 * Everything needed here is a Web Mercator projection, a grid of <img> tiles
 * and some canvas drawing.
 */

(function (global) {
    'use strict';

    var TILE_SIZE = 256;
    var MIN_ZOOM = 2;
    var MAX_ZOOM = 19;

    function clamp(value, low, high) {
        return Math.min(high, Math.max(low, value));
    }

    function worldSize(zoom) {
        return TILE_SIZE * Math.pow(2, zoom);
    }

    /* Latitude/longitude to world pixel coordinates at a given zoom. */
    function project(lat, lon, zoom) {
        var size = worldSize(zoom);
        var clampedLat = clamp(lat, -85.05112878, 85.05112878);
        var sinLat = Math.sin((clampedLat * Math.PI) / 180);
        return {
            x: ((lon + 180) / 360) * size,
            y: (0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI)) * size
        };
    }

    function unproject(x, y, zoom) {
        var size = worldSize(zoom);
        var lon = (x / size) * 360 - 180;
        var n = Math.PI - 2 * Math.PI * (y / size);
        var lat = (180 / Math.PI) * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
        return { lat: lat, lon: lon };
    }

    function SlippyMap(options) {
        this.container = options.container;
        this.tileLayer = options.tileLayer;
        this.canvas = options.canvas;
        this.ctx = this.canvas.getContext('2d');
        this.tileUrl = options.tileUrl || '';
        this.center = { lat: options.lat || 0, lon: options.lon || 0 };
        this.zoom = options.zoom || 15;
        this.follow = true;

        this.track = [];
        this.marker = null;
        this.markerStale = false;

        this.tiles = {};
        this.tileErrors = 0;
        this.tileLoads = 0;
        this.onTileTrouble = options.onTileTrouble || function () {};
        this.onViewChange = options.onViewChange || function () {};

        this._bindEvents();
        this.resize();
    }

    SlippyMap.prototype.setTileUrl = function (url) {
        if (url === this.tileUrl) {
            return;
        }
        this.tileUrl = url || '';
        this.tileLayer.innerHTML = '';
        this.tiles = {};
        this.tileErrors = 0;
        this.tileLoads = 0;
        this.render();
    };

    SlippyMap.prototype.setCenter = function (lat, lon) {
        this.center = { lat: lat, lon: lon };
        this.render();
    };

    SlippyMap.prototype.setZoom = function (zoom) {
        this.zoom = clamp(Math.round(zoom), MIN_ZOOM, MAX_ZOOM);
        this.render();
        this.onViewChange(this);
    };

    SlippyMap.prototype.setTrack = function (points) {
        this.track = points || [];
        this.render();
    };

    SlippyMap.prototype.setMarker = function (lat, lon, stale) {
        this.marker = lat === null || lon === null ? null : { lat: lat, lon: lon };
        this.markerStale = !!stale;
        if (this.marker && this.follow) {
            this.center = { lat: lat, lon: lon };
        }
        this.render();
    };

    SlippyMap.prototype.resize = function () {
        var ratio = global.devicePixelRatio || 1;
        var width = this.container.clientWidth;
        var height = this.container.clientHeight;
        this.width = width;
        this.height = height;
        this.canvas.width = Math.round(width * ratio);
        this.canvas.height = Math.round(height * ratio);
        this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        this.render();
    };

    /* ------------------------------------------------------------ interaction */

    SlippyMap.prototype._bindEvents = function () {
        var self = this;
        var dragging = false;
        var moved = 0;
        var last = null;

        this.container.addEventListener('pointerdown', function (event) {
            if (event.button !== 0) {
                return;
            }
            dragging = true;
            moved = 0;
            last = { x: event.clientX, y: event.clientY };
            self.container.setPointerCapture(event.pointerId);
        });

        this.container.addEventListener('pointermove', function (event) {
            if (!dragging) {
                return;
            }
            var dx = event.clientX - last.x;
            var dy = event.clientY - last.y;
            moved += Math.abs(dx) + Math.abs(dy);
            last = { x: event.clientX, y: event.clientY };
            if (moved > 3) {
                self.container.classList.add('dragging');
                // Dragging the map is an explicit request to stop following.
                self.follow = false;
                self.onViewChange(self);
                self._panBy(-dx, -dy);
            }
        });

        this.container.addEventListener('pointerup', function (event) {
            if (!dragging) {
                return;
            }
            dragging = false;
            self.container.classList.remove('dragging');
            try {
                self.container.releasePointerCapture(event.pointerId);
            } catch (err) {
                /* capture may already be gone */
            }
        });

        this.container.addEventListener('wheel', function (event) {
            event.preventDefault();
            var direction = event.deltaY < 0 ? 1 : -1;
            var next = clamp(self.zoom + direction, MIN_ZOOM, MAX_ZOOM);
            if (next === self.zoom) {
                return;
            }
            // Zoom about the cursor: keep the coordinate under the pointer fixed.
            var anchor = self._eventToLatLon(event);
            var rect = self.container.getBoundingClientRect();
            var offsetX = event.clientX - rect.left;
            var offsetY = event.clientY - rect.top;
            self.zoom = next;
            var anchorWorld = project(anchor.lat, anchor.lon, next);
            var centerWorld = {
                x: anchorWorld.x - (offsetX - self.width / 2),
                y: anchorWorld.y - (offsetY - self.height / 2)
            };
            var centerLatLon = unproject(centerWorld.x, centerWorld.y, next);
            self.center = { lat: centerLatLon.lat, lon: centerLatLon.lon };
            self.follow = false;
            self.onViewChange(self);
            self.render();
        }, { passive: false });

        global.addEventListener('resize', function () {
            self.resize();
        });
    };

    SlippyMap.prototype._panBy = function (dx, dy) {
        var centerWorld = project(this.center.lat, this.center.lon, this.zoom);
        var moved = unproject(centerWorld.x + dx, centerWorld.y + dy, this.zoom);
        this.center = { lat: moved.lat, lon: moved.lon };
        this.render();
    };

    SlippyMap.prototype._eventToLatLon = function (event) {
        var rect = this.container.getBoundingClientRect();
        var offsetX = event.clientX - rect.left;
        var offsetY = event.clientY - rect.top;
        var topLeft = this._topLeft();
        return unproject(topLeft.x + offsetX, topLeft.y + offsetY, this.zoom);
    };

    SlippyMap.prototype._topLeft = function () {
        var centerWorld = project(this.center.lat, this.center.lon, this.zoom);
        return { x: centerWorld.x - this.width / 2, y: centerWorld.y - this.height / 2 };
    };

    SlippyMap.prototype._toScreen = function (lat, lon) {
        var world = project(lat, lon, this.zoom);
        var topLeft = this._topLeft();
        return { x: world.x - topLeft.x, y: world.y - topLeft.y };
    };

    /* ---------------------------------------------------------------- rendering */

    SlippyMap.prototype.render = function () {
        if (!this.width || !this.height) {
            return;
        }
        this._renderTiles();
        this._renderOverlay();
    };

    SlippyMap.prototype._renderTiles = function () {
        if (!this.tileUrl) {
            this.tileLayer.innerHTML = '';
            this.tiles = {};
            return;
        }
        var self = this;
        var topLeft = this._topLeft();
        var zoom = this.zoom;
        var tileCount = Math.pow(2, zoom);
        var firstX = Math.floor(topLeft.x / TILE_SIZE);
        var firstY = Math.floor(topLeft.y / TILE_SIZE);
        var lastX = Math.floor((topLeft.x + this.width) / TILE_SIZE);
        var lastY = Math.floor((topLeft.y + this.height) / TILE_SIZE);
        var wanted = {};

        for (var ty = firstY; ty <= lastY; ty++) {
            if (ty < 0 || ty >= tileCount) {
                continue;
            }
            for (var tx = firstX; tx <= lastX; tx++) {
                // Longitude wraps; latitude does not.
                var wrappedX = ((tx % tileCount) + tileCount) % tileCount;
                var key = zoom + '/' + wrappedX + '/' + ty;
                var left = tx * TILE_SIZE - topLeft.x;
                var top = ty * TILE_SIZE - topLeft.y;
                wanted[key] = true;

                var tile = this.tiles[key];
                if (!tile) {
                    tile = document.createElement('img');
                    tile.alt = '';
                    tile.decoding = 'async';
                    tile.loading = 'eager';
                    tile.addEventListener('load', function () {
                        self.tileLoads += 1;
                    });
                    tile.addEventListener('error', function () {
                        // Tiles come from the internet via the router's WAN. If
                        // they fail the map still works, it just has no basemap,
                        // so report it once rather than retrying forever.
                        this.style.visibility = 'hidden';
                        self.tileErrors += 1;
                        if (self.tileErrors === 4 && self.tileLoads === 0) {
                            self.onTileTrouble();
                        }
                    });
                    tile.src = this.tileUrl
                        .replace('{z}', zoom)
                        .replace('{x}', wrappedX)
                        .replace('{y}', ty);
                    this.tileLayer.appendChild(tile);
                    this.tiles[key] = tile;
                }
                tile.style.left = Math.round(left) + 'px';
                tile.style.top = Math.round(top) + 'px';
            }
        }

        Object.keys(this.tiles).forEach(function (key) {
            if (!wanted[key]) {
                var stale = self.tiles[key];
                if (stale.parentNode) {
                    stale.parentNode.removeChild(stale);
                }
                delete self.tiles[key];
            }
        });
    };

    SlippyMap.prototype._renderOverlay = function () {
        var ctx = this.ctx;
        ctx.clearRect(0, 0, this.width, this.height);

        if (!this.tileUrl) {
            this._drawGraticule(ctx);
        }
        this._drawTrack(ctx);
        this._drawMarker(ctx);
        this._drawScale(ctx);
    };

    /* Reference grid shown when no basemap is configured or reachable, so the
     * map is still usable for judging relative movement. */
    SlippyMap.prototype._drawGraticule = function (ctx) {
        var step = 64;
        ctx.save();
        ctx.strokeStyle = 'rgba(147, 161, 179, 0.14)';
        ctx.lineWidth = 1;
        var topLeft = this._topLeft();
        var offsetX = -(topLeft.x % step);
        var offsetY = -(topLeft.y % step);
        for (var x = offsetX; x < this.width; x += step) {
            ctx.beginPath();
            ctx.moveTo(Math.round(x) + 0.5, 0);
            ctx.lineTo(Math.round(x) + 0.5, this.height);
            ctx.stroke();
        }
        for (var y = offsetY; y < this.height; y += step) {
            ctx.beginPath();
            ctx.moveTo(0, Math.round(y) + 0.5);
            ctx.lineTo(this.width, Math.round(y) + 0.5);
            ctx.stroke();
        }
        ctx.restore();
    };

    SlippyMap.prototype._drawTrack = function (ctx) {
        if (this.track.length < 2) {
            return;
        }
        ctx.save();
        ctx.strokeStyle = 'rgba(77, 163, 255, 0.9)';
        ctx.lineWidth = 3;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.beginPath();
        for (var i = 0; i < this.track.length; i++) {
            var point = this._toScreen(this.track[i][1], this.track[i][2]);
            if (i === 0) {
                ctx.moveTo(point.x, point.y);
            } else {
                ctx.lineTo(point.x, point.y);
            }
        }
        ctx.stroke();

        // Start of track, so a long history is readable at a glance.
        var start = this._toScreen(this.track[0][1], this.track[0][2]);
        ctx.beginPath();
        ctx.arc(start.x, start.y, 4, 0, Math.PI * 2);
        ctx.fillStyle = '#93a1b3';
        ctx.fill();
        ctx.restore();
    };

    SlippyMap.prototype._drawMarker = function (ctx) {
        if (!this.marker) {
            return;
        }
        var point = this._toScreen(this.marker.lat, this.marker.lon);
        var color = this.markerStale ? '#e5a13a' : '#35c07f';
        ctx.save();
        // Halo, so the marker stays visible over dark or busy tiles.
        ctx.beginPath();
        ctx.arc(point.x, point.y, 12, 0, Math.PI * 2);
        ctx.fillStyle = this.markerStale ? 'rgba(229, 161, 58, 0.22)' : 'rgba(53, 192, 127, 0.22)';
        ctx.fill();

        ctx.beginPath();
        ctx.arc(point.x, point.y, 6, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = '#12161c';
        ctx.stroke();
        ctx.restore();
    };

    SlippyMap.prototype._drawScale = function (ctx) {
        // Metres per pixel at the current latitude and zoom.
        var metersPerPixel =
            (156543.03392 * Math.cos((this.center.lat * Math.PI) / 180)) / Math.pow(2, this.zoom);
        var targetPx = 90;
        var rawMeters = metersPerPixel * targetPx;
        var magnitude = Math.pow(10, Math.floor(Math.log10(rawMeters)));
        var nice = magnitude;
        [1, 2, 5, 10].forEach(function (factor) {
            if (magnitude * factor <= rawMeters) {
                nice = magnitude * factor;
            }
        });
        var widthPx = nice / metersPerPixel;
        var label = nice >= 1000 ? nice / 1000 + ' km' : nice + ' m';
        var x = 12;
        var y = this.height - 34;

        ctx.save();
        ctx.font = '11px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
        ctx.fillStyle = 'rgba(18, 22, 28, 0.75)';
        ctx.fillRect(x - 4, y - 12, Math.max(widthPx, 40) + 30, 26);
        ctx.strokeStyle = '#e6ebf2';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x, y + 6);
        ctx.lineTo(x, y);
        ctx.lineTo(x + widthPx, y);
        ctx.lineTo(x + widthPx, y + 6);
        ctx.stroke();
        ctx.fillStyle = '#e6ebf2';
        ctx.fillText(label, x + widthPx + 6, y + 4);
        ctx.restore();
    };

    global.SlippyMap = SlippyMap;
    global.SlippyMap.project = project;
    global.SlippyMap.unproject = unproject;
})(window);
