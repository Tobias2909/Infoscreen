-- Kiosk controller: overlays the active screen's screens/<key>/panel.bgra + shared dim.bgra + temp badge, routes taps.
--   TAP on the LEFT panel  -> cycle to the next screen (weather -> salmon -> ... -> wrap), instantly
--   TAP on the RIGHT media -> load a new random clip/image
--   AUTO-CYCLE (900 s)     -> renders the next screen FIRST, then switches to it
-- Screens are defined in screens.conf (key:script:refresh_seconds). Adding one needs no lua edit — just a restart.
--
-- There is deliberately NO background refresh-all sweep any more. It re-rendered the 7 hidden
-- screens every 600 s (~1000 panel writes/day = 5.2 GB/day onto the SD card) purely so a switch
-- would land on a recent panel — a leftover from when weather was the only screen and that timer
-- was what refreshed it. Every screen now renders when it is about to be seen instead.
local utils = require 'mp.utils'
-- Where the project lives. kiosk.sh works it out from its own path and exports it; the
-- debug.getinfo fallback covers a hand-rolled `mpv --script=.../kiosk.lua` launch.
local DIR = os.getenv("INFOSCREEN_DIR")
if not DIR or DIR == "" then
  DIR = (debug.getinfo(1, "S").source or ""):match("^@(.*)/[^/]*$") or "."
end
local PY = "/usr/bin/python3"
local MODE_FILE = DIR .. "/panel_mode.txt"          -- persists the active screen key across reboots
local VIDEO_X = math.floor(0.62 * 1920)             -- video left edge (matches --video-margin-ratio-left=0.62); left of this = panel

-- ---- screen registry (loaded once from screens.conf) ----
local function read_screens()
  local t = {}
  local f = io.open(DIR .. "/screens.conf", "r")
  if f then
    for line in f:lines() do
      line = line:gsub("%s+", "")
      if line ~= "" and line:sub(1, 1) ~= "#" then
        local key, script, ival = line:match("([^:]+):([^:]+):?(%d*)")
        if key and script then t[#t+1] = {key = key, script = script, interval = tonumber(ival) or 0} end
      end
    end
    f:close()
  end
  if #t == 0 then t = {{key = "weather", script = "screens/weather/render_weather.py", interval = 0}} end
  return t
end
local SCREENS = read_screens()
local function screen_of(key)
  for _, s in ipairs(SCREENS) do if s.key == key then return s end end
  return SCREENS[1]
end

-- ---- active-screen state ----
local function get_key()
  local f = io.open(MODE_FILE, "r")
  if f then local m = f:read("*l"); f:close(); if m then return screen_of(m).key end end
  return SCREENS[1].key
end
local function set_key(k)
  local f = io.open(MODE_FILE, "w"); if f then f:write(k); f:close() end
end

local TEMP_W, TEMP_H, TEMP_X, TEMP_Y = 364, 64, 730, 84   -- live CPU-temp badge (top-right), under the dim
-- Shared offline banner: ONE bitmap (banner.py -> banner.bgra) overlaid on whatever screen
-- is active, instead of being baked into all 8 panel.bgra files. Overlay order must stay
-- panel(0) < temp(1) < dim(2) < banner(3): the banner sits ABOVE the dim so the alert
-- stays at full brightness at night. dim MUST stay id 2 -- renumbering it made mpv bus-error
-- once on every startup (self-healed by the respawn loop, but a real regression).
local BAN_W, BAN_H = 1190, 58
local BAN_Y = 1080 - BAN_H                                -- 1022; screens keep content above this
local banner_shown = false

-- Alert strips, both drawn by notice.py into notice_<kind>.bgra. Ids are APPEND-ONLY:
-- panel(0) < temp(1) < dim(2) < banner(3) < media(4) < render(5). Never renumber. Anything
-- above the dim stays at full brightness, which is what an alert wants.
--   media  -- bottom of the MEDIA pane, so it never collides with the offline banner, which
--            owns the same y band on the LEFT panel
--   render -- top of the LEFT panel: clear of a screen title (y66) and the CPU badge (y84-148)
local NOTICE = {
  media  = {id = 4, w = 1920 - VIDEO_X, h = 58, x = VIDEO_X, y = 1080 - 58},
  render = {id = 5, w = 1190,           h = 44, x = 0,       y = 0},
}
local notice_msg = {}          -- kind -> text currently on screen (nil = that strip is hidden)

-- overlay-add mmaps EXACTLY w*h*stride bytes of the file: if the file is shorter at that
-- instant mpv dies with SIGBUS ("Bus error"), the desktop flashes and kiosk.sh respawns it.
-- The writers are safe now (PID-unique temp file + atomic rename), but check the
-- size anyway -- this bug class has come back twice, and a short file must degrade to one
-- skipped repaint, never a crash. A handful of stats per repaint, all page-cache hits.
local function ready(path, bytes)
  local i = utils.file_info(path)
  return i ~= nil and i.is_file and i.size >= bytes
end
local function add_overlay()
  local panel = DIR .. "/screens/" .. get_key() .. "/panel.bgra"
  if ready(panel, 1190*1080*4) then
    mp.command_native({"overlay-add", 0, 0, 0, panel, 0, "bgra", 1190, 1080, 4760})
  end
  local temp = DIR .. "/temp.bgra"
  if ready(temp, TEMP_W*TEMP_H*4) then
    mp.command_native({"overlay-add", 1, TEMP_X, TEMP_Y, temp, 0, "bgra", TEMP_W, TEMP_H, TEMP_W*4})
  end
  local dim = DIR .. "/dim.bgra"
  if ready(dim, 1920*1080*4) then
    mp.command_native({"overlay-add", 2, 0, 0, dim, 0, "bgra", 1920, 1080, 7680})
  end
  local ban = DIR .. "/banner.bgra"
  if banner_shown and ready(ban, BAN_W*BAN_H*4) then
    mp.command_native({"overlay-add", 3, 0, BAN_Y, ban, 0, "bgra", BAN_W, BAN_H, BAN_W*4})
  end
  for kind, n in pairs(NOTICE) do
    local p = DIR .. "/notice_" .. kind .. ".bgra"
    if notice_msg[kind] and ready(p, n.w*n.h*4) then
      mp.command_native({"overlay-add", n.id, n.x, n.y, p, 0, "bgra", n.w, n.h, n.w*4})
    end
  end
end

-- Repaint as soon as a render lands, instead of waiting for the 30 s repaint timer. Without
-- this a tap showed the OLD panel for up to 30 s even though the fresh one was already on
-- disk a second later -- barely noticeable while the 600 s sweep kept hidden panels ~10 min
-- fresh, very noticeable now that they can be 2 h old.
local function repaint_if_active(key)
  return function() if get_key() == key then add_overlay() end end
end

-- ---- alert strips ----
local function show_notice(kind)
  local n = NOTICE[kind]
  local p = DIR .. "/notice_" .. kind .. ".bgra"
  if notice_msg[kind] and ready(p, n.w*n.h*4) then
    mp.command_native({"overlay-add", n.id, n.x, n.y, p, 0, "bgra", n.w, n.h, n.w*4})
  end
end
local function set_notice(kind, msg)
  if msg == notice_msg[kind] then return end     -- redraw only when the text really changes
  notice_msg[kind] = msg
  local n = NOTICE[kind]
  if not msg then mp.command_native({"overlay-remove", n.id}); return end
  mp.msg.warn(kind .. " notice: " .. msg)
  local f = io.open(DIR .. "/notice_" .. kind .. ".txt", "w")  -- via file, not argv: a media
  if f then f:write(msg); f:close() end                        -- filename may contain quotes
  mp.command_native_async(                                     -- show it from the completion
    {name = "subprocess", playback_only = false,               -- callback, no "wait 3 s and hope"
     args = {PY, DIR .. "/notice.py", kind, tostring(n.w), tostring(n.h)}},
    function() show_notice(kind) end)
end

-- ---- rendering ----
local last_render = {}     -- key -> mp.get_time() when its last render STARTED
local render_fail = {}     -- key -> true while that screen's last render exited non-zero
local function update_render_notice()
  local bad = {}
  for _, s in ipairs(SCREENS) do if render_fail[s.key] then bad[#bad+1] = s.key end end
  if #bad == 0 then set_notice("render", nil); return end
  local msg = "Screen failed to render: " .. bad[1]
  if #bad > 1 then msg = msg .. string.format("  (+%d more)", #bad - 1) end
  set_notice("render", msg)
end

-- Renders run as tracked mpv subprocesses instead of os.execute("python3 … &"): no shell fork,
-- mpv reaps the child, and we get a COMPLETION CALLBACK — which is what lets the auto-cycle
-- render the next screen before switching to it. playback_only=false is required: with
-- --idle=yes mpv sits idle between clips and the render must not be killed with playback.
-- THE EXIT STATUS IS CHECKED. It used to be discarded (`cb or function() end`, capture_stderr
-- off), so a screen whose python started throwing — changed API shape, expired OAuth token,
-- Pillow error — just froze at its last good panel and nothing anywhere said so. That got worse
-- when hidden screens stopped being swept, because a broken renderer no longer self-heals.
local function render(key, cb)
  last_render[key] = mp.get_time()
  mp.command_native_async(
    {name = "subprocess", playback_only = false, capture_stdout = false, capture_stderr = true,
     args = {PY, DIR .. "/" .. screen_of(key).script}},
    function(ok, res, _)
      local status = res and res.status
      local failed = not (ok and status == 0)    -- the flock "already rendering" path exits 0
      if failed ~= (render_fail[key] or false) then
        render_fail[key] = failed or nil
        update_render_notice()
      end
      if failed then
        local errtxt = (res and res.stderr or ""):gsub("%s+$", "")
        mp.msg.error(("render %s failed: status=%s %s"):format(key, tostring(status), errtxt:sub(-400)))
      end
      if cb then cb() end
    end)
end

-- ---- media ----
-- playlist.txt is an add-list of bare filenames. An entry whose file is missing used to be
-- FATAL: loadfile fails -> the playlist is empty -> mpv prints "Exiting... (Some errors
-- happened)" and quits, kiosk.sh respawns it and the display flashes. That is a 1-in-N dice
-- roll on every media change, so it fires either after hours of the 35-min auto-rotate or the
-- moment somebody taps a few times -- which is exactly how it showed up in practice: one
-- playlist line was missing its .mp4 and killed mpv roughly once per 46 media changes.
-- Three independent layers now:
--   1. mpv runs with --idle=yes, so a failed load can no longer end the process at all,
--   2. entries with no file are filtered out here, before loadfile ever sees them,
--   3. whatever is still broken is NAMED on screen (the "media" strip) instead of failing silently.
local load_fail = nil      -- file that failed to open/decode (cleared by the next good load)
local missing_note = nil   -- playlist entries with no matching file in media/
local function refresh_media_notice() set_notice("media", load_fail or missing_note) end

-- Replaces the old io.popen("grep ... | shuf"): no shell fork per media change, and the
-- missing entries it finds are what the on-screen notice names.
local pool = {}
local function rescan()
  local ok, missing = {}, {}
  local f = io.open(DIR .. "/playlist.txt", "r")
  if f then
    for line in f:lines() do
      local name = line:match("^%s*(.-)%s*$")
      if name ~= "" and name:sub(1, 1) ~= "#" then
        local i = utils.file_info(DIR .. "/media/" .. name)
        if i and i.is_file then ok[#ok+1] = name else missing[#missing+1] = name end
      end
    end
    f:close()
  end
  pool = ok
  if #ok == 0 then
    missing_note = "playlist.txt: no playable media files"
  elseif #missing == 0 then
    missing_note = nil
  else
    missing_note = "Media missing: " .. missing[1]      -- keep the label short; the filename
    if #missing > 1 then                                -- is what has to stay readable
      missing_note = missing_note .. string.format("  (+%d more)", #missing - 1)
    end
  end
  refresh_media_notice()
end
math.randomseed(os.time())
math.random(); math.random()          -- first draws after a time seed are poorly distributed

local function next_media()
  rescan()
  if #pool == 0 then mp.msg.error("playlist has no playable entries"); return end
  local m = pool[math.random(#pool)]
  mp.msg.info("TAP media -> " .. m)
  mp.commandv("loadfile", DIR .. "/media/" .. m)
end

-- A file that EXISTS but will not decode (truncated download, unsupported codec) still fails
-- the load. With --idle=yes mpv stays alive, so name it and move on. Guarded: if every entry
-- is broken this must not spin -- after RETRY_MAX consecutive failures it backs off to 60 s.
local RETRY_MAX, fails = 5, 0
-- Which file failed: capture it at start-file. Measured on this mpv (0.35.1): inside an
-- end-file error handler "path" is already nil, and the last name the lua asked for is stale
-- by then (it names the previous RECOVERY clip, not the broken one) -- so both of those
-- mis-report. The start-file value was right in 4/4 forced failures.
local loading = nil
mp.register_event("start-file", function()
  loading = ((mp.get_property("path") or ""):match("[^/]+$"))
end)
mp.register_event("end-file", function(e)
  if e.reason ~= "error" then return end
  local name = loading or "?"
  load_fail = "Media failed: " .. name
  refresh_media_notice()
  fails = fails + 1
  mp.add_timeout(fails <= RETRY_MAX and 1 or 60, next_media)
end)
mp.register_event("file-loaded", function()
  fails = 0
  if load_fail then load_fail = nil; refresh_media_notice() end
  add_overlay()
end)

-- ---- screen switching ----
local ARRIVAL_MIN = 20   -- s; a screen rendered this recently is already fresh, don't redo it
local SWITCH_CAP  = 4    -- s; the auto-cycle waits this long for the render, then switches anyway
local switch_gen  = 0    -- bumped by a tap, so a pending auto-cycle switch cannot yank you back

local function next_key()
  local k = get_key(); local idx = 1
  for i, s in ipairs(SCREENS) do if s.key == k then idx = i; break end end
  return SCREENS[(idx % #SCREENS) + 1].key   -- forward, wrap to first
end
local function land_on(nk)
  set_key(nk); mp.msg.info("screen -> " .. nk)
  add_overlay()                              -- swap to that screen's panel.bgra
end

-- TAP: switch immediately, freshen in the background. Waiting for the render (0.6-2.6 s with
-- warm caches, tens of seconds when an API is cold) would mean a touch with no visible
-- response, and the natural reaction is to tap again — skipping a screen.
local function cycle_screen()
  switch_gen = switch_gen + 1
  local nk = next_key()
  land_on(nk)
  if not last_render[nk] or (mp.get_time() - last_render[nk]) >= ARRIVAL_MIN then
    render(nk, repaint_if_active(nk))        -- paint the fresh panel the moment it lands
  end
end

-- AUTO-CYCLE: nobody is waiting on a touch, so render the next screen FIRST and switch when it
-- finishes — you always land on current data, which is what the old 600 s sweep was really for
-- (at 1/60th of the writes). Capped, because a render can hang on a slow API: render_weather
-- allows a 60 s fetch, and the rotation must not stall behind it.
local function auto_cycle()
  local nk = next_key()
  if last_render[nk] and (mp.get_time() - last_render[nk]) < ARRIVAL_MIN then
    land_on(nk); return                      -- already fresh, nothing worth waiting for
  end
  local gen, switched = switch_gen, false
  local function go()
    if gen ~= switch_gen then return end               -- a tap took over; stay where the user put us
    if switched then                                   -- cap already switched us to the stale
      if get_key() == nk then add_overlay() end        -- panel; the render just landed, paint it
      return
    end
    switched = true
    land_on(nk)
  end
  mp.add_timeout(SWITCH_CAP, go)
  render(nk, go)
end

-- ---- tap routing: panel vs media by x-position ----
-- Debounced: the touch panel fires bursts (this log used to be full of MBTN_LEFT_DBL), and a
-- burst fanned out one render + one overlay repaint per tap -- the load that made the SIGBUS
-- race fire and dropped video frames. 350 ms is below deliberate tapping, above touch chatter.
local TAP_GAP = 0.35
local last_tap = -1e9
mp.add_forced_key_binding("MBTN_LEFT", "tap", function()
  local t = mp.get_time()
  if t - last_tap < TAP_GAP then return end
  last_tap = t
  local pos = mp.get_property_native("mouse-pos")
  if pos and pos.x and pos.x >= VIDEO_X then next_media() else cycle_screen() end
end)
mp.add_forced_key_binding("MBTN_LEFT_DBL", "tap_dbl", function() end)   -- swallow: debounce already handles it, this only kills the log spam

mp.add_periodic_timer(30, function()           -- mid-dwell refresh of the ACTIVE screen, per its own interval (screens.conf 3rd field, seconds; 0 = never)
  local k = get_key(); local sc = screen_of(k)
  if sc.interval and sc.interval > 0 then
    if not last_render[k] or (mp.get_time() - last_render[k]) >= sc.interval then
      render(k, repaint_if_active(k))          -- e.g. a releases/deals page flip shows at once
    end
  end
end)
mp.add_periodic_timer(30, add_overlay)         -- cheap GPU repaint; picks up freshly-rendered panels within 30s
mp.add_periodic_timer(60, rescan)              -- so a fixed/broken playlist entry shows up (or clears) within a minute, not only on the next media change
mp.add_periodic_timer(2100, next_media)        -- media auto-rotate (35 min); tap-media just changes it early
mp.add_periodic_timer(900, auto_cycle)         -- auto-cycle screens (15 min; 8 screens = full loop every 2 h); order/wrap from screens.conf so new screens join automatically
rescan()                                       -- name a broken playlist entry immediately, without waiting for the first media change

-- ---- offline-banner watcher: show/hide the banner OVERLAY from netmon's state ----
-- The banner is an overlay (banner.py -> banner.bgra), not baked into panel.bgra, so a
-- state flip needs NO panel re-render: it appears on all 8 screens at once and survives
-- screen switches. Baking it was the bug -- only the screen that happened to re-render
-- carried it, so switching screens mid-outage made the banner disappear.
local NETLIVE = DIR .. "/screens/net/live.json"     -- written every 30 s by netmon.service
local net_online_prev = nil
local banner_drawn_at = 0
local function render_banner() os.execute(PY .. " " .. DIR .. "/banner.py >/dev/null 2>&1 &") end
local function show_banner()
  banner_shown = true
  local ban = DIR .. "/banner.bgra"
  if ready(ban, BAN_W*BAN_H*4) then
    mp.command_native({"overlay-add", 3, 0, BAN_Y, ban, 0, "bgra", BAN_W, BAN_H, BAN_W*4})
  end
end
local function hide_banner()
  banner_shown = false
  mp.command_native({"overlay-remove", 3})
end
local function raise_banner()
  render_banner()                     -- backgrounded, so give it a moment before overlaying
  banner_drawn_at = mp.get_time()
  mp.add_timeout(3, show_banner)
end
local function net_watch()
  local f = io.open(NETLIVE, "r")
  if not f then return end
  local body = f:read("*a"); f:close()
  local d = body and utils.parse_json(body)
  if not d or not d.state or not d.t then return end
  -- Mirror kiosk_common's staleness rule: once live.json is stale the screens fall back to
  -- their own ping, so drop the baseline instead of acting on a frozen value.
  if os.time() - d.t > 150 then net_online_prev = nil; return end
  local online = (d.state == "up" or d.state == "icmp_blocked")
  if net_online_prev == nil then
    if not online then raise_banner() end          -- already offline when the kiosk started
  elseif net_online_prev ~= online then
    mp.msg.info("net " .. d.state .. " (online=" .. tostring(online) .. ") -> banner " ..
                (online and "off" or "on"))
    if online then hide_banner() else raise_banner() end
  elseif not online and mp.get_time() - banner_drawn_at > 300 then
    render_banner()                                -- refresh wording (e.g. date rollover)
    banner_drawn_at = mp.get_time()
  end
  net_online_prev = online
end
net_watch()
mp.add_periodic_timer(30, net_watch)

-- ---- live CPU temp badge (overlay id 1, UNDER the dim; one source updated on a timer ->
--      identical on every screen, unlike the old per-panel baked temp) ----
-- Cadence is 60 s, not 15 s: temp_badge.py skips the write while the whole-degree
-- value is unchanged, but the CPU temp jitters enough that only 28% of 15 s ticks were skipped
-- (0.39 GB/day of SD writes + 5760 python spawns/day for a number that nobody watches tick).
local function render_temp() os.execute(PY .. " " .. DIR .. "/temp_badge.py >/dev/null 2>&1 &") end
local function show_temp()
  local temp = DIR .. "/temp.bgra"
  if ready(temp, TEMP_W*TEMP_H*4) then
    mp.command_native({"overlay-add", 1, TEMP_X, TEMP_Y, temp, 0, "bgra", TEMP_W, TEMP_H, TEMP_W*4})
  end
end
render_temp()
mp.add_periodic_timer(60, function() render_temp(); show_temp() end)
