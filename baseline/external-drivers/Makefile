# Build external-baseline drivers. Run from this dir. Adjust LG if deps/ moves.
LG ?= ../../deps/LiveGraph
CXX ?= g++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra -Wpedantic

.PHONY: all livegraph livegraph_stream livegraph_digest livegraph_p10 clean

all: livegraph livegraph_stream livegraph_digest

livegraph: livegraph_driver.cpp
	$(CXX) $(CXXFLAGS) -I$(LG)/bind $< -L$(LG)/build -llivegraph -lpthread -o livegraph_driver
	@echo "built livegraph_driver (run with LD_LIBRARY_PATH=$(LG)/build)"

livegraph_stream: livegraph_driver_stream.cpp
	$(CXX) $(CXXFLAGS) -I$(LG)/bind $< -L$(LG)/build -llivegraph -lpthread -o livegraph_driver_stream
	@echo "built livegraph_driver_stream (run with LD_LIBRARY_PATH=$(LG)/build)"

livegraph_digest: livegraph_digest_driver.cpp
	$(CXX) $(CXXFLAGS) -I$(LG)/bind $< -L$(LG)/build -llivegraph -lpthread -o livegraph_digest_driver
	@echo "built livegraph_digest_driver (run with LD_LIBRARY_PATH=$(LG)/build)"

livegraph_p10: livegraph_p10_driver.cpp
	$(CXX) $(CXXFLAGS) -I$(LG)/bind $< -L$(LG)/build -llivegraph -ldl -lpthread -o livegraph_p10_driver
	@echo "built livegraph_p10_driver (run with LD_LIBRARY_PATH=$(LG)/build)"

clean:
	rm -f livegraph_driver livegraph_digest_driver livegraph_p10_driver
