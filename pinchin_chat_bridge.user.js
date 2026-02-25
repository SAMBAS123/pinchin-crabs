// ==UserScript==
// @name         PINCHIN Chat Bridge
// @namespace    https://pump.fun
// @version      2.0
// @description  Forwards pump.fun live chat to Crab Sim via localhost
// @match        https://pump.fun/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-start
// ==/UserScript==

(function() {
    'use strict';

    var BRIDGE_URL = 'http://127.0.0.1:8420/chat';
    var bridgeOk = true;
    var msgCount = 0;
    var seenMsgs = {};

    console.log('[PINCHIN] === SCRIPT STARTING v2 ===');

    function sendToBridge(user, msg) {
        var key = user + ':' + msg;
        if (seenMsgs[key]) return;
        seenMsgs[key] = true;
        var payload = JSON.stringify({ user: user, msg: msg });
        try {
            if (typeof GM_xmlhttpRequest !== 'undefined') {
                GM_xmlhttpRequest({
                    method: 'POST',
                    url: BRIDGE_URL,
                    data: payload,
                    headers: { 'Content-Type': 'application/json' },
                    onload: function() { bridgeOk = true; },
                    onerror: function() {
                        if (bridgeOk) {
                            console.log('[PINCHIN] Bridge offline');
                            bridgeOk = false;
                        }
                    }
                });
            }
        } catch(e) {
            console.log('[PINCHIN] Send error: ' + e);
        }
        msgCount++;
    }

    // Hook ALL WebSockets
    var OrigWS = window.WebSocket;
    window.WebSocket = function(url, protocols) {
        console.log('[PINCHIN] WS OPEN: ' + url);
        var ws = protocols ? new OrigWS(url, protocols) : new OrigWS(url);

        ws.addEventListener('message', function(event) {
            try {
                var data = event.data;
                if (typeof data !== 'string') return;

                // Socket.IO event messages
                if (data.startsWith('42')) {
                    var jsonStr = data.replace(/^42\d*/, '');
                    var parsed = JSON.parse(jsonStr);
                    var eventName = parsed[0];
                    var payload = parsed[1];
                    console.log('[PINCHIN] WS event: ' + eventName);

                    if (eventName === 'newMessage' && payload) {
                        var user = (payload.username || 'anon').substring(0, 16);
                        var msg = (payload.message || '').substring(0, 100);
                        console.log('[PINCHIN] CHAT: ' + user + ': ' + msg);
                        sendToBridge(user, msg);
                    }
                }

                // Socket.IO ack with history
                if (data.match(/^43\d/)) {
                    var prefixMatch = data.match(/^(43\d*)/);
                    var jsonStr2 = data.substring(prefixMatch[1].length);
                    var parsed2 = JSON.parse(jsonStr2);
                    if (Array.isArray(parsed2) && parsed2[0]) {
                        var msgs = parsed2[0];
                        if (Array.isArray(msgs)) {
                            msgs.slice(-8).forEach(function(m) {
                                if (m && m.username && m.message) {
                                    sendToBridge(m.username.substring(0, 16), m.message.substring(0, 100));
                                }
                            });
                        } else if (msgs && msgs.messages) {
                            msgs.messages.slice(-8).forEach(function(m) {
                                if (m && m.username && m.message) {
                                    sendToBridge(m.username.substring(0, 16), m.message.substring(0, 100));
                                }
                            });
                        }
                    }
                }
            } catch(e) {}
        });

        return ws;
    };
    window.WebSocket.prototype = OrigWS.prototype;
    window.WebSocket.CONNECTING = OrigWS.CONNECTING;
    window.WebSocket.OPEN = OrigWS.OPEN;
    window.WebSocket.CLOSING = OrigWS.CLOSING;
    window.WebSocket.CLOSED = OrigWS.CLOSED;

    // Badge
    window.addEventListener('load', function() {
        setTimeout(function() {
            var badge = document.createElement('div');
            badge.id = 'pinchin-bridge-badge';
            badge.style.cssText = 'position:fixed;bottom:10px;right:10px;background:#1a1a2e;color:#0f0;padding:6px 12px;border-radius:8px;font-family:monospace;font-size:12px;z-index:99999;border:1px solid #333;cursor:pointer;';
            badge.textContent = 'CRAB SIM BRIDGE';
            badge.title = 'Click to test';
            badge.onclick = function() {
                sendToBridge('system', 'Bridge test!');
                badge.style.borderColor = bridgeOk ? '#0f0' : '#f00';
                setTimeout(function() { badge.style.borderColor = '#333'; }, 1000);
            };
            document.body.appendChild(badge);
            setInterval(function() {
                badge.textContent = bridgeOk ? 'BRIDGE ON (' + msgCount + ')' : 'BRIDGE OFF';
                badge.style.color = bridgeOk ? '#0f0' : '#f00';
            }, 3000);
        }, 2000);
    });

    console.log('[PINCHIN] === HOOKS INSTALLED ===');
})();
