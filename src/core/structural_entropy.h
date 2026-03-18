#pragma once

#include <cstdint>
#include <vector>
#include <optional>
#include <functional>
#include "forward_window_result.h"
#include "trajectory.h"

namespace sep {
namespace structural {

// Forward declarations
struct StructuralOptions;
struct StructuralResult;
class StructuralProcessor;
class EntropyProcessor;
struct StructuralEvent;
struct StructuralAggregateEvent;

// Forward declaration for DampedValue
namespace bitspace {
    struct DampedValue;
}

/**
 * Structural State enumeration
 * Represents the stability state of the signal manifold.
 */
enum class StructuralState {
    NULL_STATE,
    STABLE,
    UNSTABLE,
    COLLAPSING,
    COLLAPSED,
    RECOVERING,
    OSCILLATION,    // Formerly FLIP
    REGIME_SHIFT    // Formerly RUPTURE
};

/**
 * Structural Event structure
 */
struct StructuralEvent {
    uint32_t index{0};
    StructuralState state{StructuralState::NULL_STATE};
    uint8_t bit_prev{0};
    uint8_t bit_curr{0};
    
    bool operator==(const StructuralEvent& other) const;
};

/**
* Transform bitstream into rich Structural events
*/
std::vector<StructuralEvent> transform_rich(const std::vector<uint8_t>& bits);

/**
* Aggregate Structural events into grouped events
*/
std::vector<StructuralAggregateEvent> aggregate(const std::vector<StructuralEvent>& events);

/**
 * Structural Aggregate Event
 */
struct StructuralAggregateEvent {
    uint32_t index{0};
    StructuralState state{StructuralState::NULL_STATE};
    uint32_t count{1};
};

/**
 * Configuration options for Structural Entropy processing
 */
struct StructuralOptions {
    double coherence_threshold = 0.7;
    double stability_threshold = 0.8;
    double collapse_threshold = 0.5;
    int max_iterations = 1000;
    bool enable_damping = true;
    double damping_factor = 0.95;
    double entropy_weight = 0.30;
    double coherence_weight = 0.20;
};

/**
 * Result structure for Structural operations
 */
struct StructuralResult {
    double coherence = 0.0;
    double stability = 0.0;
    double confidence = 0.0;
    bool collapse_detected = false;
    double rupture_ratio = 0.0; // Keep ratio name for continuity or change to shift_ratio? "rupture" is still descriptive.
    StructuralState final_state = StructuralState::STABLE;
    std::vector<StructuralEvent> events;
    
    // Additional members
    double collapse_threshold = 0.5;
    std::vector<StructuralAggregateEvent> aggregated_events;
    uint32_t null_state_count = 0;
    uint32_t oscillation_count = 0;
    uint32_t shift_count = 0;
    double oscillation_ratio = 0.0;
    double entropy = 0.0;
};

/**
 * Base Structural processor
 */
class StructuralProcessor {
public:
    StructuralProcessor() = default;
    virtual ~StructuralProcessor() = default;
    
    virtual std::optional<StructuralState> process(uint8_t current_bit);
    virtual void reset();
    
protected:
    std::optional<uint8_t> prev_bit;
};

/**
 * Entropy-based processor implementation
 */
class EntropyProcessor : public StructuralProcessor {
public:
    explicit EntropyProcessor(const StructuralOptions& options);
    ~EntropyProcessor() override = default;
    
    StructuralResult analyze(const std::vector<uint8_t>& data);
    void reset() override;
    
    // Additional methods equality
    bitspace::DampedValue integrateFutureTrajectories(const std::vector<uint8_t>& bitstream, size_t current_index);
    double matchKnownPaths(const std::vector<double>& trajectory);
    std::vector<uint8_t> convertToBits(const std::vector<uint32_t>& data);
    double calculateCosineSimilarity(const std::vector<double>& a, const std::vector<double>& b);
    
    std::optional<StructuralState> detectTransition(uint32_t prev_bit, uint32_t current_bit);
    bool detectCollapse(const StructuralResult& result) const;
    
private:
    StructuralOptions options_;
    StructuralState current_state_ = StructuralState::STABLE;
    uint32_t prev_bit_ = 0;
};

namespace bitspace {
namespace structural {
    constexpr double DEFAULT_LAMBDA = 0.1;
    constexpr int MAX_PACKAGE_SIZE = 1024;
    constexpr int MIN_PACKAGE_SIZE = 8;
}
} // namespace bitspace

} // namespace structural
} // namespace sep