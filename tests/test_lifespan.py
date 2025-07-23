import asyncio
import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from restate.server import asgi_app, lifespan_processor


class MockEndpoint:
    
    def __init__(self):
        self.identity_keys = []


@pytest.fixture
def mock_endpoint():
    return MockEndpoint()


@pytest.fixture
def base_lifespan_scope():
    return {
        'type': 'lifespan',
        'asgi': {'version': '3.0', 'spec_version': '2.1'},
        'state': {}
    }


class TestLifespanProcessor:
    @pytest.mark.asyncio
    async def test_successful_lifespan_without_state(self):
        startup_called = False
        shutdown_called = False
        
        @asynccontextmanager
        async def lifespan():
            nonlocal startup_called, shutdown_called
            startup_called = True
            yield None
            shutdown_called = True
        
        scope = {'type': 'lifespan'}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        receive_mock.side_effect = [
            {'type': 'lifespan.startup'},
            {'type': 'lifespan.shutdown'}
        ]
        
        await lifespan_processor(scope, receive_mock, send_mock, lifespan)
        
        assert startup_called
        assert shutdown_called
        
        expected_calls = [
            {'type': 'lifespan.startup.complete'},
            {'type': 'lifespan.shutdown.complete'}
        ]
        
        assert send_mock.call_count == 2
        for i, expected_call in enumerate(expected_calls):
            send_mock.assert_any_call(expected_call)

    @pytest.mark.asyncio
    async def test_successful_lifespan_with_state(self):
        test_state = {'key': 'value', 'counter': 42}
        
        @asynccontextmanager
        async def lifespan():
            yield test_state
        
        scope = {'type': 'lifespan', 'state': {}}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        receive_mock.side_effect = [
            {'type': 'lifespan.startup'},
            {'type': 'lifespan.shutdown'}
        ]
        
        await lifespan_processor(scope, receive_mock, send_mock, lifespan)
        
        assert scope['state'] == test_state
        
        expected_calls = [
            {'type': 'lifespan.startup.complete'},
            {'type': 'lifespan.shutdown.complete'}
        ]
        
        assert send_mock.call_count == 2
        for expected_call in expected_calls:
            send_mock.assert_any_call(expected_call)

    @pytest.mark.asyncio
    async def test_lifespan_with_state_but_no_state_support(self):
        @asynccontextmanager
        async def lifespan():
            yield {'key': 'value'}
        
        scope = {'type': 'lifespan'}
        scope = {'type': 'lifespan'}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        receive_mock.side_effect = [
            {'type': 'lifespan.startup'},
        ]
        
        with pytest.raises(RuntimeError, match="The server does not support state in lifespan"):
            await lifespan_processor(scope, receive_mock, send_mock, lifespan)
        
        send_mock.assert_called_once()
        call_args = send_mock.call_args[0][0]
        assert call_args['type'] == 'lifespan.startup.failed'
        assert 'The server does not support state in lifespan' in call_args['message']

    @pytest.mark.asyncio
    async def test_startup_exception(self):
        @asynccontextmanager
        async def lifespan():
            raise ValueError("Startup error")
            yield  # pragma: no cover
        
        scope = {'type': 'lifespan'}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        receive_mock.side_effect = [
            {'type': 'lifespan.startup'},
        ]
        
        with pytest.raises(ValueError, match="Startup error"):
            await lifespan_processor(scope, receive_mock, send_mock, lifespan)
        
        # Should send startup failed
        send_mock.assert_called_once()
        call_args = send_mock.call_args[0][0]
        assert call_args['type'] == 'lifespan.startup.failed'
        assert 'ValueError: Startup error' in call_args['message']

    @pytest.mark.asyncio
    async def test_shutdown_exception(self):
        @asynccontextmanager
        async def lifespan():
            yield None
            raise RuntimeError("Shutdown error")
        
        scope = {'type': 'lifespan'}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        receive_mock.side_effect = [
            {'type': 'lifespan.startup'},
            {'type': 'lifespan.shutdown'}
        ]
        
        with pytest.raises(RuntimeError, match="Shutdown error"):
            await lifespan_processor(scope, receive_mock, send_mock, lifespan)
        
        # Should send startup complete, then shutdown failed
        assert send_mock.call_count == 2
        
        # First call should be startup complete
        first_call = send_mock.call_args_list[0][0][0]
        assert first_call['type'] == 'lifespan.startup.complete'
        
        # Second call should be shutdown failed
        second_call = send_mock.call_args_list[1][0][0]
        assert second_call['type'] == 'lifespan.shutdown.failed'
        assert 'RuntimeError: Shutdown error' in second_call['message']

    @pytest.mark.asyncio
    async def test_async_context_manager_exception_in_enter(self):
        """Test exception in async context manager __aenter__."""
        class FailingContextManager:
            async def __aenter__(self):
                raise ConnectionError("Failed to connect")
            
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass  # pragma: no cover
        
        @asynccontextmanager
        async def lifespan():
            async with FailingContextManager():
                yield None
        
        scope = {'type': 'lifespan'}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        receive_mock.side_effect = [
            {'type': 'lifespan.startup'},
        ]
        
        with pytest.raises(ConnectionError, match="Failed to connect"):
            await lifespan_processor(scope, receive_mock, send_mock, lifespan)
        
        # Should send startup failed
        send_mock.assert_called_once()
        call_args = send_mock.call_args[0][0]
        assert call_args['type'] == 'lifespan.startup.failed'
        assert 'ConnectionError: Failed to connect' in call_args['message']

    @pytest.mark.asyncio
    async def test_multiple_receive_calls(self):
        """Test that receive is called exactly twice (startup and shutdown)."""
        @asynccontextmanager
        async def lifespan():
            yield None
        
        scope = {'type': 'lifespan'}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        receive_mock.side_effect = [
            {'type': 'lifespan.startup'},
            {'type': 'lifespan.shutdown'}
        ]
        
        await lifespan_processor(scope, receive_mock, send_mock, lifespan)
        
        assert receive_mock.call_count == 2


class TestASGIAppLifespan:

    @pytest.mark.asyncio
    async def test_lifespan_scope_with_lifespan_handler(self, mock_endpoint):
        @asynccontextmanager
        async def lifespan():
            yield None
        
        app = asgi_app(mock_endpoint, lifespan=lifespan)
        
        scope = {'type': 'lifespan'}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        receive_mock.side_effect = [
            {'type': 'lifespan.startup'},
            {'type': 'lifespan.shutdown'}
        ]
        
        await app(scope, receive_mock, send_mock)
        
        # Should handle lifespan events
        assert send_mock.call_count == 2
        send_mock.assert_any_call({'type': 'lifespan.startup.complete'})
        send_mock.assert_any_call({'type': 'lifespan.shutdown.complete'})

    @pytest.mark.asyncio
    async def test_lifespan_scope_without_lifespan_handler(self, mock_endpoint):
        """Test that lifespan scope is ignored when no lifespan handler is provided."""
        app = asgi_app(mock_endpoint, lifespan=None)
        
        scope = {'type': 'lifespan'}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        # This should not call lifespan_processor and should continue to http handling
        # Since it's a lifespan scope but no handler, it should raise NotImplementedError
        with pytest.raises(NotImplementedError, match="Unknown scope type lifespan"):
            await app(scope, receive_mock, send_mock)

    @pytest.mark.asyncio
    async def test_lifespan_early_return(self, mock_endpoint):
        """Test that lifespan handling returns early and doesn't continue to HTTP processing."""
        call_order = []
        
        @asynccontextmanager
        async def lifespan():
            call_order.append('lifespan_start')
            yield None
            call_order.append('lifespan_end')
        
        app = asgi_app(mock_endpoint, lifespan=lifespan)
        
        scope = {'type': 'lifespan'}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        receive_mock.side_effect = [
            {'type': 'lifespan.startup'},
            {'type': 'lifespan.shutdown'}
        ]
        
        await app(scope, receive_mock, send_mock)
        
        # Verify lifespan was called
        assert 'lifespan_start' in call_order
        assert 'lifespan_end' in call_order
        
        # Verify it returned early (sent lifespan events, not HTTP events)
        assert send_mock.call_count == 2
        calls = [call[0][0] for call in send_mock.call_args_list]
        assert any(call['type'] == 'lifespan.startup.complete' for call in calls)
        assert any(call['type'] == 'lifespan.shutdown.complete' for call in calls)
        assert not any(call['type'] == 'http.response.start' for call in calls)


class TestLifespanEdgeCases:
    @pytest.mark.asyncio
    async def test_lifespan_with_complex_state(self):
        complex_state = {
            'database': {'connection': 'mock_db_connection'},
            'cache': {'redis': 'mock_redis_connection'},
            'config': {
                'debug': True,
                'workers': 4,
                'nested': {'deep': {'value': 'test'}}
            },
            'services': ['service1', 'service2']
        }
        
        @asynccontextmanager
        async def lifespan():
            yield complex_state
        
        scope = {'type': 'lifespan', 'state': {}}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        receive_mock.side_effect = [
            {'type': 'lifespan.startup'},
            {'type': 'lifespan.shutdown'}
        ]
        
        await lifespan_processor(scope, receive_mock, send_mock, lifespan)
        
        # Verify complex state was set correctly
        assert scope['state'] == complex_state
        assert scope['state']['database']['connection'] == 'mock_db_connection'
        assert scope['state']['config']['nested']['deep']['value'] == 'test'

    @pytest.mark.asyncio
    async def test_lifespan_with_async_operations(self):
        startup_delay = 0.01
        shutdown_delay = 0.01
        
        @asynccontextmanager
        async def lifespan():
            # Simulate async startup
            await asyncio.sleep(startup_delay)
            yield {'initialized': True}
            # Simulate async shutdown
            await asyncio.sleep(shutdown_delay)
        
        scope = {'type': 'lifespan', 'state': {}}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        receive_mock.side_effect = [
            {'type': 'lifespan.startup'},
            {'type': 'lifespan.shutdown'}
        ]
        
        start_time = asyncio.get_event_loop().time()
        await lifespan_processor(scope, receive_mock, send_mock, lifespan)
        end_time = asyncio.get_event_loop().time()
        
        # Should have taken at least the sum of delays
        assert end_time - start_time >= startup_delay + shutdown_delay
        assert scope['state'] == {'initialized': True}

    @pytest.mark.asyncio
    async def test_lifespan_exception_with_detailed_traceback(self):
        def inner_function():
            raise ValueError("Inner error with details")
        
        @asynccontextmanager
        async def lifespan():
            inner_function()
            yield None  # pragma: no cover
        
        scope = {'type': 'lifespan'}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        receive_mock.side_effect = [
            {'type': 'lifespan.startup'},
        ]
        
        with pytest.raises(ValueError):
            await lifespan_processor(scope, receive_mock, send_mock, lifespan)
        
        # Check that traceback is included
        send_mock.assert_called_once()
        call_args = send_mock.call_args[0][0]
        assert call_args['type'] == 'lifespan.startup.failed'
        assert 'inner_function' in call_args['message']
        assert 'ValueError: Inner error with details' in call_args['message']
        assert 'Traceback' in call_args['message']

    @pytest.mark.asyncio
    async def test_lifespan_state_mutation_during_execution(self):
        @asynccontextmanager
        async def lifespan():
            state = {'counter': 0, 'items': []}
            yield state
            # State might be mutated by the application
            # This tests that we don't interfere with state mutations
        
        scope = {'type': 'lifespan', 'state': {}}
        receive_mock = AsyncMock()
        send_mock = AsyncMock()
        
        receive_mock.side_effect = [
            {'type': 'lifespan.startup'},
            {'type': 'lifespan.shutdown'}
        ]
        
        await lifespan_processor(scope, receive_mock, send_mock, lifespan)
        
        scope['state']['counter'] = 42
        scope['state']['items'].append('test')
        
        assert scope['state']['counter'] == 42
        assert scope['state']['items'] == ['test']
