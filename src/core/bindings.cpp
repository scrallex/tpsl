#include "byte_stream_manifold.h"
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <string>
#include <vector>

namespace py = pybind11;

std::string analyze_bytes(const py::bytes &input_bytes, size_t window_bytes,
                          size_t step_bytes, int signature_precision) {
  std::string str_bytes = static_cast<std::string>(input_bytes);
  std::vector<uint8_t> bytes(str_bytes.begin(), str_bytes.end());
  sep::ByteStreamConfig config;
  config.window_bits = window_bytes * 8;
  config.step_bits = std::max<size_t>(1, step_bytes * 8);
  config.signature_precision = signature_precision;

  sep::ByteStreamManifold manifold = sep::analyze_byte_stream(bytes, config);
  return manifold.to_json(config).dump();
}

PYBIND11_MODULE(manifold_engine, m) {
  m.doc() = "Manifold Engine C++ Extension";
  m.def("analyze_bytes", &analyze_bytes, py::arg("input_bytes"),
        py::arg("window_bytes") = 64, py::arg("step_bytes") = 48,
        py::arg("signature_precision") = 3);
}
