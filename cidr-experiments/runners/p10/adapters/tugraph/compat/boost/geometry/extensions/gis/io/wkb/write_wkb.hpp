#pragma once

// See read_wkb.hpp in this directory.  This shim exists only because the
// installed TuGraph public umbrella header includes an obsolete WKB-v1 path.
// The CIDR P10 worker never instantiates a spatial type or WKB operation.
