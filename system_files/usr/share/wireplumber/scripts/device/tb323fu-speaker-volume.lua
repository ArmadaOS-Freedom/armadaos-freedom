-- SPDX-License-Identifier: MIT

local devinfo = require("device-info-cache")

local hook = SimpleEventHook {
  name = "device/tb323fu-speaker-volume",
  after = {
    "device/find-stored-routes",
    "device/find-best-routes",
    "device/apply-route-props",
  },
  before = { "device/apply-routes" },
  interests = { EventInterest { Constraint { "event.type", "=", "select-routes" } } },
  execute = function(event)
    local device = event:get_subject()
    if device.properties["device.name"] ~= "alsa_card.platform-sound" then return end
    if device.properties["api.alsa.card.longname"] ~= "LENOVO-TB323FU" then return end
    local routes = event:get_data("selected-routes")
    local info = devinfo:get_device_info(device)
    if not routes or not info then return end
    for device_id, raw in pairs(routes) do
      local route = Json.Raw(raw):parse()
      local ri = devinfo.find_route_info(info, route, false)
      -- The hidden speaker route should pass unity gain to the amplifier.
      if ri and ri.direction == "Output" and ri.name == "[Out] Speaker"
          and (not route.props or not route.props.channelVolumes) then
        routes[device_id] = Json.Raw(raw):merge(Json.Raw('{"props":{"channelVolumes":[1.0,1.0]}}')):to_string()
        event:set_data("selected-routes", routes)
        return
      end
    end
  end
}

hook:register()
