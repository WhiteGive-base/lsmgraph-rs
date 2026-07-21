#pragma once

// TuGraph 4.5.2's public lgraph_spatial.h includes the removed experimental
// Boost.Geometry WKB-v1 header even when no spatial API is used.  P10 only
// exercises graph transactions and edge iterators, so this deliberately empty
// compile-time shim makes that unused include resolvable.  It does not provide
// or claim WKB functionality.
