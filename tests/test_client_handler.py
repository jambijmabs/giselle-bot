import unittest
from unittest.mock import Mock, patch
from handlers import handle_client_message

class TestClientHandler(unittest.TestCase):
    def setUp(self):
        self.phone = "whatsapp:+5219988103956"
        self.conversation_state = {self.phone: {'history': [], 'client_name': None, 'name_asked': 0}}
        self.client = Mock()
        self.message_handler = Mock()
        self.utils = Mock()
        self.recontact_handler = Mock()

    @patch('handlers.logger')
    def test_handle_client_message_success(self, mock_logger):
        result = handle_client_message(
            self.phone, "hola", 0, None, None, self.conversation_state,
            self.client, self.message_handler, self.utils, self.recontact_handler
        )
        self.assertEqual(result, ("Mensaje enviado", 200))
        mock_logger.debug.assert_called()

    @patch('handlers.logger')
    def test_handle_client_message_key_error(self, mock_logger):
        del self.conversation_state[self.phone]
        result = handle_client_message(
            self.phone, "hola", 0, None, None, self.conversation_state,
            self.client, self.message_handler, self.utils, self.recontact_handler
        )
        self.assertEqual(result, ("Error in conversation state", 500))
        mock_logger.error.assert_called()

if __name__ == '__main__':
    unittest.main()
