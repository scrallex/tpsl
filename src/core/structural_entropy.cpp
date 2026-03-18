#include "structural_entropy.h"
#include "standard_includes.h"
#include "trajectory.h"

#include <atomic>
#include <cmath>
#include <execution>
#include <iostream>
#include <numeric>
#include <optional>
#include <vector>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

namespace sep::structural {

bool StructuralEvent::operator==(const StructuralEvent &other) const {
  return index == other.index && state == other.state &&
         bit_prev == other.bit_prev && bit_curr == other.bit_curr;
}

std::vector<StructuralEvent> transform_rich(const std::vector<uint8_t> &bits) {
  if (bits.size() < 2)
    return {};

  const std::size_t n = bits.size() - 1;
  std::vector<StructuralEvent> result(n);
  std::vector<std::size_t> idx(n);
  std::iota(idx.begin(), idx.end(), 0);

  std::atomic<bool> invalid{false};

  std::for_each(
      std::execution::par_unseq, idx.begin(), idx.end(), [&](std::size_t i) {
        uint8_t prev = bits[i];
        uint8_t curr = bits[i + 1];
        if ((prev != 0 && prev != 1) || (curr != 0 && curr != 1)) {
          invalid.store(true, std::memory_order_relaxed);
          return;
        }
        StructuralState st = StructuralState::NULL_STATE;
        if ((prev == 0 && curr == 1) || (prev == 1 && curr == 0)) {
          st = StructuralState::OSCILLATION;
        } else if (prev == 1 && curr == 1) {
          st = StructuralState::REGIME_SHIFT;
        }
        result[i] = StructuralEvent{static_cast<uint32_t>(i), st, prev, curr};
      });

  if (invalid.load(std::memory_order_relaxed))
    return {};
  return result;
}

std::vector<StructuralAggregateEvent>
aggregate(const std::vector<StructuralEvent> &events) {
  if (events.empty()) {
    return {};
  }
  std::vector<StructuralAggregateEvent> aggregated;
  aggregated.push_back({events[0].index, events[0].state, 1});
  for (size_t i = 1; i < events.size(); ++i) {
    if (events[i].state == aggregated.back().state) {
      aggregated.back().count++;
    } else {
      aggregated.push_back({events[i].index, events[i].state, 1});
    }
  }
  return aggregated;
}

std::optional<StructuralState>
StructuralProcessor::process(uint8_t current_bit) {
  if (current_bit != 0 && current_bit != 1) {
    return std::nullopt;
  }
  if (!prev_bit.has_value()) {
    prev_bit = current_bit;
    return std::nullopt;
  }
  uint8_t prev = prev_bit.value();
  std::optional<StructuralState> event_state;
  if (prev == 0 && current_bit == 0) {
    event_state = StructuralState::NULL_STATE;
  } else if ((prev == 0 && current_bit == 1) ||
             (prev == 1 && current_bit == 0)) {
    event_state = StructuralState::OSCILLATION;
  } else if (prev == 1 && current_bit == 1) {
    event_state = StructuralState::REGIME_SHIFT;
  }
  prev_bit = current_bit;
  return event_state;
}

void StructuralProcessor::reset() { prev_bit.reset(); }

bitspace::DampedValue EntropyProcessor::integrateFutureTrajectories(
    const std::vector<uint8_t> &bitstream, size_t current_index) {
  bitspace::DampedValue damped_value;

  if (current_index >= bitstream.size()) {
    damped_value.final_value = 0.0;
    damped_value.confidence = 0.0;
    return damped_value;
  }

  size_t window_size =
      std::min(static_cast<size_t>(20), bitstream.size() - current_index);
  std::vector<uint8_t> local_window(bitstream.begin() + current_index,
                                    bitstream.begin() + current_index +
                                        window_size);

  auto local_events = transform_rich(local_window);
  double local_entropy = 0.5;
  double local_coherence = 0.5;

  if (!local_events.empty()) {
    int null_count = 0, oscillation_count = 0, shift_count = 0;
    int stable_count = 0, unstable_count = 0, collapsing_count = 0;
    int collapsed_count = 0, recovering_count = 0;
    for (const auto &event : local_events) {
      switch (event.state) {
      case StructuralState::NULL_STATE:
        null_count++;
        break;
      case StructuralState::STABLE:
        stable_count++;
        break;
      case StructuralState::UNSTABLE:
        unstable_count++;
        break;
      case StructuralState::COLLAPSING:
        collapsing_count++;
        break;
      case StructuralState::COLLAPSED:
        collapsed_count++;
        break;
      case StructuralState::RECOVERING:
        recovering_count++;
        break;
      case StructuralState::OSCILLATION:
        oscillation_count++;
        break;
      case StructuralState::REGIME_SHIFT:
        shift_count++;
        break;
      }
    }

    float total = static_cast<float>(local_events.size());
    float null_ratio = null_count / total;
    float oscillation_ratio = oscillation_count / total;
    float shift_ratio = shift_count / total;

    // Other states are derived or transient in the simple transformer, but we
    // count them for entropy completeness if they occurred
    float stable_ratio = stable_count / total;
    float unstable_ratio = unstable_count / total;
    float collapsing_ratio = collapsing_count / total;
    float collapsed_ratio = collapsed_count / total;
    float recovering_ratio = recovering_count / total;

    auto safe_log2 = [](float x) -> float {
      return (x > 0.0f) ? std::log2(x) : 0.0f;
    };
    local_entropy = -(null_ratio * safe_log2(null_ratio) +
                      oscillation_ratio * safe_log2(oscillation_ratio) +
                      shift_ratio * safe_log2(shift_ratio) +
                      stable_ratio * safe_log2(stable_ratio) +
                      unstable_ratio * safe_log2(unstable_ratio) +
                      collapsing_ratio * safe_log2(collapsing_ratio) +
                      collapsed_ratio * safe_log2(collapsed_ratio) +
                      recovering_ratio * safe_log2(recovering_ratio));
    local_entropy = std::fmax(0.05, std::fmin(1.0, local_entropy / 1.585));
    local_coherence = 1.0 - local_entropy;
  }

  double k1 = options_.entropy_weight;
  double k2 = options_.coherence_weight;
  double lambda = k1 * local_entropy + k2 * (1.0 - local_coherence);
  lambda = std::fmax(0.01, std::fmin(1.0, lambda));
  damped_value.lambda = lambda;
  damped_value.start_index = current_index;

  double accumulated_value = 0.0;
  double current_bit = static_cast<double>(bitstream[current_index]);

  damped_value.path.clear();
  damped_value.path.reserve(bitstream.size() - current_index);
  damped_value.path.push_back(current_bit);

  for (size_t j = current_index + 1; j < bitstream.size(); ++j) {
    double future_bit = static_cast<double>(bitstream[j]);
    double time_difference = static_cast<double>(j - current_index);
    double weight = std::exp(-lambda * time_difference);

    double contribution = (future_bit - current_bit) * weight;
    accumulated_value += contribution;

    damped_value.path.push_back(accumulated_value);
  }

  damped_value.final_value = accumulated_value;

  if (damped_value.path.size() > 2) {
    double trajectory_variance = 0.0;
    double mean_trajectory = 0.0;
    for (double val : damped_value.path)
      mean_trajectory += val;
    mean_trajectory /= damped_value.path.size();

    for (double val : damped_value.path)
      trajectory_variance += std::pow(val - mean_trajectory, 2);
    trajectory_variance /= damped_value.path.size();

    double stability_score = 1.0 / (1.0 + trajectory_variance);
    damped_value.confidence = std::fmax(0.0, std::fmin(1.0, stability_score));
  } else {
    damped_value.confidence = 0.5;
  }

  damped_value.converged = (damped_value.confidence > 0.7);
  return damped_value;
}

double
EntropyProcessor::matchKnownPaths(const std::vector<double> &current_path) {
  if (current_path.size() < 3)
    return 0.5;

  double best_similarity = 0.0;

  std::vector<double> exponential_pattern;
  exponential_pattern.reserve(current_path.size());
  double initial_value = current_path[0];
  for (size_t i = 0; i < current_path.size(); ++i) {
    double expected_value =
        initial_value * std::exp(-0.1 * static_cast<double>(i));
    exponential_pattern.push_back(expected_value);
  }

  double exp_similarity =
      calculateCosineSimilarity(current_path, exponential_pattern);
  best_similarity = std::max(best_similarity, exp_similarity);

  if (current_path.size() >= 2) {
    std::vector<double> linear_pattern;
    linear_pattern.reserve(current_path.size());
    double slope = (current_path.back() - current_path.front()) /
                   (current_path.size() - 1);
    for (size_t i = 0; i < current_path.size(); ++i) {
      double expected_value = current_path[0] + slope * static_cast<double>(i);
      linear_pattern.push_back(expected_value);
    }

    double linear_similarity =
        calculateCosineSimilarity(current_path, linear_pattern);
    best_similarity = std::max(best_similarity, linear_similarity);
  }

  std::vector<double> oscillating_pattern;
  oscillating_pattern.reserve(current_path.size());
  double amplitude = (current_path.back() - current_path.front()) / 2.0;
  double mean_value = (current_path.back() + current_path.front()) / 2.0;
  for (size_t i = 0; i < current_path.size(); ++i) {
    double expected_value =
        mean_value + amplitude * std::sin(2.0 * M_PI * i / current_path.size());
    oscillating_pattern.push_back(expected_value);
  }

  double osc_similarity =
      calculateCosineSimilarity(current_path, oscillating_pattern);
  best_similarity = std::max(best_similarity, osc_similarity);

  return std::fmax(0.0, std::fmin(1.0, best_similarity));
}

EntropyProcessor::EntropyProcessor(const StructuralOptions &options)
    : options_(options) {}

StructuralResult EntropyProcessor::analyze(const std::vector<uint8_t> &bits) {
  StructuralResult result;
  result.collapse_threshold = options_.collapse_threshold;

  result.events = transform_rich(bits);

  result.aggregated_events = aggregate(result.events);

  for (const auto &event : result.events) {
    switch (event.state) {
    case StructuralState::NULL_STATE:
      result.null_state_count++;
      break;
    case StructuralState::STABLE:
      break;
    case StructuralState::UNSTABLE:
      break;
    case StructuralState::COLLAPSING:
      break;
    case StructuralState::COLLAPSED:
      break;
    case StructuralState::RECOVERING:
      break;
    case StructuralState::OSCILLATION:
      result.oscillation_count++;
      break;
    case StructuralState::REGIME_SHIFT:
      result.shift_count++;
      break;
    default:
      break;
    }
  }

  if (!result.events.empty()) {
    result.rupture_ratio = static_cast<float>(result.shift_count) /
                           static_cast<float>(result.events.size());
    result.oscillation_ratio = static_cast<float>(result.oscillation_count) /
                               static_cast<float>(result.events.size());
  }

  if (!result.events.empty()) {
    float null_ratio = static_cast<float>(result.null_state_count) /
                       static_cast<float>(result.events.size());
    float oscillation_ratio = result.oscillation_ratio;
    float shift_ratio = result.rupture_ratio;

    auto safe_log2 = [](float x) -> float {
      return (x > 0.0f) ? std::log2(x) : 0.0f;
    };

    result.entropy = -(null_ratio * safe_log2(null_ratio) +
                       oscillation_ratio * safe_log2(oscillation_ratio) +
                       shift_ratio * safe_log2(shift_ratio));

    result.entropy = std::fmax(0.05f, std::fmin(1.0f, result.entropy / 1.585f));

    float pattern_coherence = 1.0f - result.entropy;
    float stability_factor = 1.0f - result.rupture_ratio;
    float consistency_factor = 1.0f - result.oscillation_ratio;

    result.coherence = pattern_coherence * 0.6f + stability_factor * 0.3f +
                       consistency_factor * 0.1f;
    result.coherence = std::fmax(0.01f, std::fmin(0.99f, result.coherence));
  }

  if (!bits.empty() && bits.size() > 10) {
    bitspace::DampedValue dv = integrateFutureTrajectories(bits, 0);
    double trajectory_confidence = matchKnownPaths(dv.path);

    float pattern_coherence = result.coherence;
    float trajectory_coherence = static_cast<float>(trajectory_confidence);

    result.coherence = 0.3f * trajectory_coherence + 0.7f * pattern_coherence;

    if (std::abs(dv.final_value) < 2.0) {
      float stability_factor =
          1.0f / (1.0f + 0.1f * std::abs(static_cast<float>(dv.final_value)));
      result.coherence = result.coherence * stability_factor;
    }

    result.coherence = std::fmax(0.0f, std::fmin(1.0f, result.coherence));
  }

  result.collapse_detected =
      (result.rupture_ratio >= options_.collapse_threshold);

  return result;
}

bool EntropyProcessor::detectCollapse(const StructuralResult &result) const {
  return result.collapse_detected ||
         result.rupture_ratio >= options_.collapse_threshold;
}

std::vector<uint8_t>
EntropyProcessor::convertToBits(const std::vector<uint32_t> &values) {
  std::vector<uint8_t> bits;
  bits.reserve(values.size() * 32);

  for (uint32_t value : values) {
    for (int i = 0; i < 32; ++i) {
      bits.push_back((value >> i) & 1);
    }
  }
  return bits;
}

double
EntropyProcessor::calculateCosineSimilarity(const std::vector<double> &a,
                                            const std::vector<double> &b) {
  if (a.size() != b.size() || a.empty())
    return 0.0;

  double dot_product = 0.0;
  double norm_a = 0.0;
  double norm_b = 0.0;

  for (size_t i = 0; i < a.size(); ++i) {
    dot_product += a[i] * b[i];
    norm_a += a[i] * a[i];
    norm_b += b[i] * b[i];
  }

  norm_a = std::sqrt(norm_a);
  norm_b = std::sqrt(norm_b);

  if (norm_a == 0.0 || norm_b == 0.0)
    return 0.0;
  return dot_product / (norm_a * norm_b);
}

void EntropyProcessor::reset() {
  StructuralProcessor::reset();
  current_state_ = StructuralState::STABLE;
  prev_bit_ = 0;
}

} // namespace sep::structural
