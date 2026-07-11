#pragma once

#include <vector>

#include "wrapper_base.h"

namespace esphome {
namespace ct002 {

// Adds a single, runtime-adjustable *total* offset (watts) to the grid reading.
// Mirrors src/astrameter/powermeter/wrappers/dynamic_offset.py.
//
// On ESPHome a *static* offset is applied natively via a `sensor: filters:
// offset:` on the upstream power sensor (see CONTRIBUTING.md — transform.py has
// no C++ counterpart). This wrapper adds a *live* offset on top of that, set
// over MQTT (the ct002 device command `{"grid_offset": <w>}`) and surfaced as
// the "Grid Offset" HA Number.
//
// The offset is a total adjustment spread evenly across the phases, so the
// summed grid reading (what active control targets) shifts by exactly the
// offset — not by N× on an N-phase meter. It shifts the value the control loop
// / balancer see; the raw reading (get_powermeter_watts_raw, used by the
// Marstek app and cloud reporting) passes through untouched so those consumers
// still match the physical meter.
class DynamicOffsetPowermeter : public PowermeterWrapper {
 public:
  explicit DynamicOffsetPowermeter(Powermeter *wrapped, float offset = 0.0f)
      : PowermeterWrapper(wrapped), offset_(offset) {}

  std::vector<float> get_powermeter_watts() override {
    std::vector<float> values = this->wrapped_->get_powermeter_watts();
    if (this->offset_ != 0.0f && !values.empty()) {
      const float share = this->offset_ / static_cast<float>(values.size());
      for (float &v : values) v += share;
    }
    return values;
  }

  float offset() const { return this->offset_; }
  void set_offset(float offset) { this->offset_ = offset; }

 protected:
  float offset_;
};

}  // namespace ct002
}  // namespace esphome
