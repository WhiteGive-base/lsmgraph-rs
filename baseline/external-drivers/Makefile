# Build external-baseline drivers. Run from this dir. Adjust LG if deps/ moves.
LG := ../../deps/LiveGraph
CXX := g++
CXXFLAGS := -O2 -std=c++17 -Wall
.PHONY: all livegraph clean
all: livegraph
livegraph: livegraph_driver.cpp
	$(CXX) $(CXXFLAGS) -I$(LG)/bind $< -L$(LG)/build -llivegraph -lpthread -o livegraph_driver
	@echo "built livegraph_driver (run with LD_LIBRARY_PATH=$(LG)/build)"
clean:
	rm -f livegraph_driver
