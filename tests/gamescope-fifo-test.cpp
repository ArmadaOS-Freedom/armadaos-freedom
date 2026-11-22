// Standalone C++17 test for the applied Gamescope FIFO selector.
//
// Build/run in the manager-controlled ARM64 Docker environment against the
// real applied source tree (no copied selector implementation):
//
//   g++ -std=c++17 -I "$GAMESCOPE_SOURCE/src" tests/gamescope-fifo-test.cpp \
//       -o "$TASK_OUTPUT/gamescope-fifo-test"
//   "$TASK_OUTPUT/gamescope-fifo-test"
//
// $GAMESCOPE_SOURCE is the checkout with the applied 0037 patch; the FIFO
// selector is now provided by the 3.17 tree that the include resolves against.

#include <cassert>
#include <cstdint>
#include <iostream>
#include <memory>
#include <vector>

#include "Utils/CommitQueue.h"

namespace
{

struct FixtureEntry
{
	uint64_t commitID;
	uint32_t surf;
	bool fifo;
	bool done;
	bool abandoned;
	struct
	{
		uint32_t flags = 0;
	} present_timing;
};

using Entry = std::shared_ptr<FixtureEntry>;
using Queue = std::vector<Entry>;
using gamescope::CommitQueue::SelectionResult;
using gamescope::CommitQueue::Select;

Entry makeEntry( uint64_t commitID, uint32_t surf, bool fifo, bool done = false, bool abandoned = false )
{
	return std::make_shared<FixtureEntry>( FixtureEntry{ commitID, surf, fifo, done, abandoned } );
}

void testMissingID()
{
	Queue queue{ makeEntry( 1, 10, true ) };
	const auto selection = Select( queue, 99 );

	assert( selection.first == SelectionResult::Missing );
	assert( selection.second == queue.end() );
}

void testFirstFIFOReady()
{
	Queue queue{ makeEntry( 11, 10, true ) };
	const auto selection = Select( queue, 11 );

	assert( selection.first == SelectionResult::Ready );
	assert( selection.second == queue.begin() );
	assert( ( *selection.second )->commitID == 11 );
}

void testLaterSameSurfaceFIFOBlocked()
{
	Queue queue{ makeEntry( 21, 10, true ), makeEntry( 22, 10, true ) };
	const auto selection = Select( queue, 22 );

	assert( selection.first == SelectionResult::Blocked );
	assert( selection.second == queue.begin() + 1 );
	assert( ( *selection.second )->commitID == 22 );
}

void testBlockedFIFOReadyOnceEarlierDone()
{
	Queue queue{ makeEntry( 31, 10, true ), makeEntry( 32, 10, true ) };
	queue[0]->done = true;

	const auto selection = Select( queue, 32 );

	assert( selection.first == SelectionResult::Ready );
	assert( selection.second == queue.begin() + 1 );
}

void testEarlierAbandonedDoesNotBlock()
{
	Queue queue{ makeEntry( 41, 10, true, false, true ), makeEntry( 42, 10, true ) };
	const auto selection = Select( queue, 42 );

	assert( selection.first == SelectionResult::Ready );
	assert( selection.second == queue.begin() + 1 );
}

void testEarlierOtherSurfaceFIFODoesNotBlock()
{
	Queue queue{ makeEntry( 51, 11, true ), makeEntry( 52, 10, true ) };
	const auto selection = Select( queue, 52 );

	assert( selection.first == SelectionResult::Ready );
	assert( selection.second == queue.begin() + 1 );
}

void testEarlierNonFIFODoesNotBlock()
{
	Queue queue{ makeEntry( 61, 10, false ), makeEntry( 62, 10, true ) };
	const auto selection = Select( queue, 62 );

	assert( selection.first == SelectionResult::Ready );
	assert( selection.second == queue.begin() + 1 );
}

void testSelectedNonFIFONotBlocked()
{
	Queue queue{ makeEntry( 71, 10, true ), makeEntry( 72, 10, false ) };
	const auto selection = Select( queue, 72 );

	assert( selection.first == SelectionResult::Ready );
	assert( selection.second == queue.begin() + 1 );
}

void testAnyEarlierUnfinishedSameSurfaceFIFOBlocks()
{
	Queue queue{
		makeEntry( 81, 10, true, true ),
		makeEntry( 82, 10, true ),
		makeEntry( 83, 10, true ),
	};
	const auto selection = Select( queue, 83 );

	assert( selection.first == SelectionResult::Blocked );
	assert( selection.second == queue.begin() + 2 );
}

void testLookupDoesNotChangeQueue()
{
	Queue queue{ makeEntry( 91, 10, true ), makeEntry( 92, 10, true ), makeEntry( 93, 20, false ) };
	const Entry first = queue[0];
	const Entry second = queue[1];
	const Entry third = queue[2];

	const auto missing = Select( queue, 999 );
	const auto ready = Select( queue, 91 );
	const auto blocked = Select( queue, 92 );

	assert( missing.first == SelectionResult::Missing );
	assert( ready.first == SelectionResult::Ready );
	assert( blocked.first == SelectionResult::Blocked );

	assert( queue.size() == 3 );
	assert( queue[0] == first && queue[1] == second && queue[2] == third );
	assert( queue[0]->commitID == 91 );
	assert( queue[1]->commitID == 92 );
	assert( queue[2]->commitID == 93 );
	assert( ready.second == queue.begin() );
	assert( blocked.second == queue.begin() + 1 );
}

} // namespace

int main()
{
	testMissingID();
	testFirstFIFOReady();
	testLaterSameSurfaceFIFOBlocked();
	testBlockedFIFOReadyOnceEarlierDone();
	testEarlierAbandonedDoesNotBlock();
	testEarlierOtherSurfaceFIFODoesNotBlock();
	testEarlierNonFIFODoesNotBlock();
	testSelectedNonFIFONotBlocked();
	testAnyEarlierUnfinishedSameSurfaceFIFOBlocks();
	testLookupDoesNotChangeQueue();

	std::cout << "gamescope-fifo-test: all 10 FIFO selector cases passed\n";
	return 0;
}
