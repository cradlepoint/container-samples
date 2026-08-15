/* UI controller: polls the API and renders the readout, map marker and track. */

(function () {
    'use strict';

    var STATUS_INTERVAL_MS = 1000;
    var HISTORY_INTERVAL_MS = 10000;

    var el = {};
    var map = null;
    var state = {
        lastPointTs: null,
        track: [],
        centered: false,
        tileUrl: '',
        stickyUntil: 0
    };

    function byId(id) {
        return document.getElementById(id);
    }

    function cacheElements() {
        [
            'map', 'tiles', 'overlay', 'banner', 'fixDot', 'fixLabel', 'gpsdDot', 'gpsdLabel',
            'lat', 'lon', 'alt', 'speed', 'heading', 'sats', 'accuracy', 'age', 'fixState',
            'trackPoints', 'trackPersist', 'nmeaClients', 'uptime',
            'followToggle', 'zoomIn', 'zoomOut', 'clearHistory', 'mapNote',
            'pollInterval', 'staleAfter', 'tileUrl', 'saveSettings'
        ].forEach(function (id) {
            el[id] = byId(id);
        });
    }

    function fmt(value, digits, suffix) {
        if (value === null || value === undefined || value === '') {
            return '--';
        }
        var num = Number(value);
        if (!isFinite(num)) {
            return '--';
        }
        return num.toFixed(digits) + (suffix || '');
    }

    function fmtClock(epochSeconds) {
        var date = new Date(epochSeconds * 1000);
        return date.toLocaleTimeString();
    }

    function fmtDuration(seconds) {
        if (seconds === null || seconds === undefined) {
            return '--';
        }
        var total = Math.floor(seconds);
        var days = Math.floor(total / 86400);
        var hours = Math.floor((total % 86400) / 3600);
        var minutes = Math.floor((total % 3600) / 60);
        var secs = total % 60;
        if (days > 0) {
            return days + 'd ' + hours + 'h';
        }
        if (hours > 0) {
            return hours + 'h ' + minutes + 'm';
        }
        if (minutes > 0) {
            return minutes + 'm ' + secs + 's';
        }
        return secs + 's';
    }

    function request(method, url, body) {
        var options = { method: method, headers: {} };
        if (body !== undefined) {
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(body);
        }
        return fetch(url, options).then(function (response) {
            if (!response.ok) {
                return response.json().catch(function () {
                    return { error: 'HTTP ' + response.status };
                }).then(function (payload) {
                    throw new Error(payload.error || 'HTTP ' + response.status);
                });
            }
            return response.json();
        });
    }

    /* Messages from a user action are held for a few seconds so the once-a-second
     * status poll cannot wipe them before they have been read. */
    function showBanner(message, sticky) {
        if (!message) {
            if (state.stickyUntil && Date.now() < state.stickyUntil) {
                return;
            }
            state.stickyUntil = 0;
            el.banner.hidden = true;
            return;
        }
        if (sticky) {
            state.stickyUntil = Date.now() + 8000;
        }
        el.banner.textContent = message;
        el.banner.hidden = false;
    }

    /* ------------------------------------------------------------------ status */

    function renderStatus(data) {
        var fix = data.fix || {};
        var lastValid = data.last_valid;

        // Distinguish "no GPS lock" from "no Config Store". Without the volume
        // the router looks identical to a receiver that never gets a fix.
        if (data.config_store_ok === false) {
            showBanner(
                'Cannot read the router Config Store. Add the $CONFIG_STORE volume to this ' +
                'service in the container project; no GPS data is available until then.'
            );
        } else {
            showBanner(null);
        }

        if (fix.valid) {
            el.fixDot.className = 'dot good';
            el.fixLabel.textContent = 'GPS fix';
            el.fixState.textContent = 'Live fix, ' + (fix.satellites || 0) + ' satellites';
        } else if (lastValid) {
            el.fixDot.className = 'dot warn';
            el.fixLabel.textContent = 'No fix';
            el.fixState.textContent = 'Stale. Last fix ' + fmtClock(lastValid.sampled_at);
        } else {
            el.fixDot.className = 'dot bad';
            el.fixLabel.textContent = 'No fix';
            el.fixState.textContent = 'Waiting for first fix';
        }

        // The readout follows the live fix when there is one, otherwise it shows
        // the last known position clearly marked as stale. It never presents an
        // old position as current.
        var shown = fix.valid ? fix : lastValid;
        el.lat.textContent = shown ? fmt(shown.latitude, 6, ' deg') : '--';
        el.lon.textContent = shown ? fmt(shown.longitude, 6, ' deg') : '--';
        el.alt.textContent = shown ? fmt(shown.altitude_m, 1, ' m') : '--';
        el.speed.textContent = shown ? fmt(shown.speed_kph, 1, ' km/h') : '--';
        el.heading.textContent = shown ? fmt(shown.heading, 0, ' deg') : '--';
        el.sats.textContent = fix.satellites === undefined ? '--' : String(fix.satellites);
        el.accuracy.textContent = shown ? fmt(shown.accuracy_m, 1, ' m') : '--';
        el.age.textContent = fix.age_s === null || fix.age_s === undefined ? '--' : fmt(fix.age_s, 0, ' s');

        var gpsd = data.gpsd || {};
        el.gpsdDot.className = 'dot ' + (gpsd.reachable ? 'good' : 'bad');
        el.gpsdLabel.textContent = 'gpsd :' + (gpsd.port || '--');

        var track = data.track || {};
        el.trackPoints.textContent = (track.points || 0) + ' / ' + (track.max_points || 0);
        el.trackPersist.textContent = track.persistent ? 'volume' : 'tmp (not persistent)';
        el.nmeaClients.textContent = String((data.nmea || {}).clients || 0);
        el.uptime.textContent = fmtDuration(data.uptime_s);

        if (data.tile_url !== state.tileUrl) {
            state.tileUrl = data.tile_url || '';
            el.tileUrl.value = state.tileUrl;
            map.setTileUrl(state.tileUrl);
            showBanner(null);
        }

        var position = fix.valid ? fix : lastValid;
        if (position && position.latitude !== null && position.longitude !== null) {
            if (!state.centered) {
                map.setCenter(position.latitude, position.longitude);
                state.centered = true;
            }
            map.setMarker(position.latitude, position.longitude, !fix.valid);
        } else {
            map.setMarker(null, null, false);
        }
    }

    /* ----------------------------------------------------------------- history */

    function loadHistory(incremental) {
        var url = '/api/history';
        if (incremental && state.lastPointTs !== null) {
            url += '?since=' + encodeURIComponent(state.lastPointTs);
        }
        return request('GET', url).then(function (data) {
            var points = data.points || [];
            if (!incremental) {
                state.track = points;
            } else if (points.length) {
                state.track = state.track.concat(points);
            }
            if (state.track.length) {
                state.lastPointTs = state.track[state.track.length - 1][0];
            }
            map.setTrack(state.track);
        });
    }

    /* ----------------------------------------------------------------- settings */

    function loadSettings() {
        return request('GET', '/api/config').then(function (cfg) {
            el.pollInterval.value = cfg.gps_poll_interval;
            el.staleAfter.value = cfg.gps_stale_after;
            el.tileUrl.value = cfg.tile_url || '';
            state.tileUrl = cfg.tile_url || '';
            map.setTileUrl(state.tileUrl);
        });
    }

    function saveSettings() {
        el.saveSettings.disabled = true;
        request('POST', '/api/config', {
            gps_poll_interval: el.pollInterval.value,
            gps_stale_after: el.staleAfter.value,
            tile_url: el.tileUrl.value.trim()
        }).then(function () {
            showBanner(null);
            return loadSettings();
        }).catch(function (error) {
            showBanner('Could not save settings: ' + error.message, true);
        }).then(function () {
            el.saveSettings.disabled = false;
        });
    }

    /* --------------------------------------------------------------------- init */

    function poll(fn, interval) {
        var run = function () {
            fn().catch(function (error) {
                showBanner('API error: ' + error.message, true);
            }).then(function () {
                window.setTimeout(run, interval);
            });
        };
        run();
    }

    function init() {
        cacheElements();

        map = new SlippyMap({
            container: el.map,
            tileLayer: el.tiles,
            canvas: el.overlay,
            zoom: 15,
            tileUrl: '',
            onTileTrouble: function () {
                el.mapNote.hidden = false;
            },
            onViewChange: function (instance) {
                el.followToggle.textContent = instance.follow ? 'Following' : 'Follow';
                el.followToggle.classList.toggle('primary', instance.follow);
            }
        });

        el.followToggle.classList.add('primary');
        el.followToggle.addEventListener('click', function () {
            map.follow = !map.follow;
            el.followToggle.textContent = map.follow ? 'Following' : 'Follow';
            el.followToggle.classList.toggle('primary', map.follow);
            if (map.follow && map.marker) {
                map.setCenter(map.marker.lat, map.marker.lon);
            }
        });
        el.zoomIn.addEventListener('click', function () {
            map.setZoom(map.zoom + 1);
        });
        el.zoomOut.addEventListener('click', function () {
            map.setZoom(map.zoom - 1);
        });

        el.clearHistory.addEventListener('click', function () {
            if (!window.confirm('Delete all recorded location history?')) {
                return;
            }
            request('POST', '/api/history/clear', {}).then(function () {
                state.track = [];
                state.lastPointTs = null;
                map.setTrack([]);
            }).catch(function (error) {
                showBanner('Could not clear history: ' + error.message, true);
            });
        });

        el.saveSettings.addEventListener('click', saveSettings);

        loadSettings().catch(function (error) {
            showBanner('Could not load settings: ' + error.message, true);
        });

        loadHistory(false).catch(function () {
            /* reported by the poller below */
        });

        poll(function () {
            return request('GET', '/api/status').then(renderStatus);
        }, STATUS_INTERVAL_MS);

        poll(function () {
            return loadHistory(true);
        }, HISTORY_INTERVAL_MS);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
