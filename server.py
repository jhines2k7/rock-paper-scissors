import sys
import logging
import uuid
import ssl
import time
import threading
import json

from gevent import monkey
monkey.patch_all()

from flask import Flask, request
from flask_socketio import SocketIO, join_room, emit, leave_room
from flask_cors import CORS

from web3 import Web3
from web3.middleware import geth_poa_middleware
from eth_account import Account

import argparse
from dotenv import load_dotenv
import os

import requests
from decimal import Decimal
import math

logging.basicConfig(
  stream=sys.stderr,
  level=logging.DEBUG,
  format='%(levelname)s:%(asctime)s:%(message)s'
)

logger = logging.getLogger(__name__)

# Create a parser for the command-line arguments
# e.g. python your_script.py --env .env.prod
parser = argparse.ArgumentParser(description='Loads variables from the specified .env file and prints them.')
parser.add_argument('--env', type=str, default='.env.local', help='The .env file to load')
args = parser.parse_args()
# Load the .env file specified in the command-line arguments
load_dotenv(args.env)
HTTP_PROVIDER = os.getenv("HTTP_PROVIDER")
CONTRACT_OWNER_PRIVATE_KEY = os.getenv("CONTRACT_OWNER_PRIVATE_KEY")
logger.info(f"Contract owner private key: {CONTRACT_OWNER_PRIVATE_KEY}")
ARBITER_PRIVATE_KEY = os.getenv("ARBITER_PRIVATE_KEY")
logger.info(f"Arbiter private key: {ARBITER_PRIVATE_KEY}")
RPS_CONTRACT_FACTORY_ADDRESS = os.getenv("RPS_CONTRACT_FACTORY_ADDRESS")
# Connection
web3 = Web3(Web3.HTTPProvider(HTTP_PROVIDER))

if 'test' in args.env:
  logger.info("Running on testnet")
  # # for Goerli, make sure to inject the poa middleware
  web3.middleware_onion.inject(geth_poa_middleware, layer=0)

# Load and parse the contract ABIs.
rps_contract_factory_abi = None
rps_contract_abi = None
with open('contracts/RPSContractFactory.json') as f:
  factory_json = json.load(f)
  rps_contract_factory_abi = factory_json['abi']
with open('contracts/RPSContract.json') as f:
  rps_json = json.load(f)
  contract_abi = rps_json['abi']


app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
CORS(app)
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins='*')
games = {}
disconnected_players = {}

def clean_disconnected_players():
  while True:
    time.sleep(30)  # check every 30 seconds
    current_time = time.time()

    for player_id, player_info in list(disconnected_players.items()):
      if current_time - player_info['timestamp'] > 30:
        del disconnected_players[player_id]
        logger.info('Removed disconnected player information for player ID: {}'.format(player_id))

def determine_winner(player1_choice, player2_choice):
  if player1_choice == player2_choice:
    return 'draw'
  elif (player1_choice == 'rock' and player2_choice == 'scissors') or \
    (player1_choice == 'paper' and player2_choice == 'rock') or \
    (player1_choice == 'scissors' and player2_choice == 'paper'):
    return 'player1'
  else:
    return 'player2'

def new_game_session():
  session_id = uuid.uuid4()
  games[session_id] = {
    'gameId': str(session_id),
    'player1': {
      'id': None,
      'choice': None,
      'wager': None,
      'winnings': None,
      'totalWinnings': 0,
      'wagerAccepted': False,
      'wagerOffered': False,
      'winner': None,
      'playerAddress': None,
      'contractAddress': None,
      'joinContract': {
        'confirmationNumber': None,
        'transactionHash': None,
        'receipt': None
      },
      'decideWinner': {
        'confirmationNumber': None,
        'transactionHash': None,
        'receipt': None
      }
    },
    'player2': {
      'id': None,
      'choice': None,
      'wager': None,
      'winnings': None,
      'totalWinnings': 0,
      'wagerAccepted': False,
      'wagerOffered': False,
      'winner': None,
      'playerAddress': None,
      'contractAddress': None,
      'joinContract': {
        'confirmationNumber': None,
        'transactionHash': None,
        'receipt': None
      },
      'decideWinner': {
        'confirmationNumber': None,
        'transactionHash': None,
        'receipt': None
      }
    }
  }

  logger.info('Current game state in new_game_session: %s', games[session_id])

  return session_id

def get_open_game():
  for session_id, game in games.items():
    if game['player1']['id'] is not None and game['player2']['id'] is None:
      return session_id

  return None

def get_empty_player():
  return {
    'id': None,
    'choice': None,
    'wager': None,
    'winnings': None,
    'totalWinnings': 0,
    'wagerAccepted': False,
    'wagerOffered': False,
    'winner': None,
    'playerAddress': None,
    'contractAddress': None,
    'joinContract': {
      'confirmationNumber': None,
      'transactionHash': None,
      'receipt': None
    },
    'decideWinner': {
      'confirmationNumber': None,
      'transactionHash': None,
      'receipt': None
    }
  }

def get_player_by_id(game, player_id):
  if game['player1']['id'] == player_id:
    return 'player1', game['player1']
  if game['player2']['id'] == player_id:
    return 'player2', game['player2']
  return None, None

def get_opponent_by_id(game, player_id):
  if game['player1']['id'] == player_id:
    return 'player2', game['player2']
  if game['player2']['id'] == player_id:
    return 'player1', game['player1']
  return None, None

def set_winner(game, winner):
  if winner == 'player1':
    game['player1']['winner'] = True
    game['player2']['winner'] = False
    return
  if winner == 'player2':
    game['player1']['winner'] = False
    game['player2']['winner'] = True
    return
  if winner == 'draw':
    game['player1']['winner'] = None
    game['player2']['winner'] = None
    return
  return

def delete_game_session(game_session_id):
  """Delete the game session with the given game_session_id."""
  if game_session_id in games:
    del games[game_session_id]
    logger.info("Game session with ID {} has been deleted.".format(game_session_id))
  else:
    logger.warning("Unable to find game session with ID {}.".format(game_session_id))

@socketio.on('join_contract_confirmation_number_received')
def handle_join_contract_confirmation_number_received(data):
  logger.info('join contract confirmation number: %s', data)

@socketio.on('join_contract_transaction_hash_received')
def handle_join_contract_transaction(data):
  logger.info('join contract transaction hash: %s', data)
  game = get_game_by_player_id(data['playerId'])
  player = get_player_by_id(game, data['playerId'])
  player['joinContract']['transactionHash'] = data['transactionHash']
  player['address'] = data['address']
  player['contractAddress'] = data['contractAddress']
  logger.info('Current game state in on join_contract_transaction_hash_received: %s', games[data['sessionId']])

@socketio.on('join_contract_transaction_receipt_received')
def handle_join_contract_transaction(data):
  logger.info('join contract transaction receipt received: %s', data)  

@socketio.on('connect')
def handle_connect():
  player_id = request.sid

  open_game_session = get_open_game()
  logger.info('Open game session: {}'.format(open_game_session))

  if open_game_session is None:
    logger.info('No open games. Creating new game session.')
    game_session_id = new_game_session()
    games[game_session_id]['player1']['id'] = player_id
  else:
    game_session_id = open_game_session
    open_game = games[open_game_session]

    logger.info('Joining open game: {}'.format(open_game))

    if not open_game['player1']['id']:
      open_game['player1']['id'] = player_id
    elif not open_game['player2']['id']:
      open_game['player2']['id'] = player_id

  join_room(str(game_session_id))

  opponent_key, opponent = get_opponent_by_id(games[game_session_id], player_id)
  opponent_id = None if opponent is None else opponent['id']

  emit('connected', {
      'sessionId': str(game_session_id),
      'playerId': player_id,
      'opponentId': opponent_id
  }, room=player_id)

  if opponent_id is not None:
      emit('opponent_connected', {
          'opponentId': player_id
      }, room=opponent_id)

  logger.info('Player ID: {}, Opponent ID: {}'.format(player_id, opponent_id))
  logger.info('Current game state in on connect handler: %s', games[game_session_id])

@socketio.on('attempt_reconnect')
def handle_attempt_reconnect():
  player_id = request.sid
  open_game_session = get_open_game()

  if open_game_session is None:
    logger.info('No open games. Creating new game session.')
    game_session_id = new_game_session()
    games[game_session_id]['player1']['id'] = player_id
  else:
    logger.info('Joining open game session.')
    game_session_id = open_game_session
    open_game = games[open_game_session]

    if not open_game['player1']['id']:
      open_game['player1']['id'] = player_id
    elif not open_game['player2']['id']:
      open_game['player2']['id'] = player_id

  join_room(str(game_session_id))  # Add the player to the room corresponding to the game session
  _, player = get_player_by_id(games[game_session_id], player_id)
  opponent_key, opponent = get_opponent_by_id(games[game_session_id], player_id)

  emit('connected', {
      'sessionId': str(game_session_id),
      'playerId': player['id'],
      'opponentId': opponent['id'] if opponent else None
    }, room=player_id)

  if opponent:
    emit('opponent_connected', {
      'opponentId': player_id
    }, room=opponent['id'])

  logger.info('Current game state in on attempt_reconnect: %s', games[game_session_id])
  logger.info(f'Player {player_id} reconnected.')

@socketio.on('refresh')
def handle_refresh():
  player_id = request.sid

  # Send an opponent_refresh event to the opponent if available.
  game_session_id, game = get_game_by_player_id(player_id)
  player_key, player = get_player_by_id(game, player_id)
  opponent_key, opponent = get_opponent_by_id(game, player_id)

  if opponent:
    logger.info("Player {} refreshed. Informing opponent.".format(player_id))
    emit('opponent_refresh', room=opponent["id"])
  else:
    # No opponent found, meaning there's only one player in the session.
    logger.info("Player {} refreshed with no opponent. Deleting game/session.".format(player_id))
    delete_game_session(game_session_id)

@socketio.on('offer_wager')
def handle_submit_wager(data):
  logger.info('Received wager from player %s: %s', request.sid, data)
  player_id = request.sid
  wager = data['wager']

  game_session_id = None
  for session_id, game in games.items():
    if game['player1']['id'] == player_id or game['player2']['id'] == player_id:
      game_session_id = session_id
      break

  game = games[game_session_id]
  player_key, current_player = get_player_by_id(game, player_id)
  _, opponent_player = get_opponent_by_id(game, player_id)

  if current_player:
    current_player['wager'] = wager
    current_player['wagerOffered'] = True
    opponent_id = opponent_player['id']

  logger.info('Current game state in on offer_wager: %s', games[game_session_id])
  emit('wager_offered', {'wager': wager, 'opponent_id': player_id}, room=opponent_id)

def get_game_by_player_id(player_id):
  for session_id, game in games.items():
    player1_id = game['player1']['id']
    player2_id = game['player2']['id']
    if player1_id == player_id or player2_id == player_id:
      return session_id, game
  return None, None

@socketio.on('accept_wager')
def handle_accept_wager():
  player_id = request.sid
  game_session_id, game = get_game_by_player_id(player_id)

  if game is None:
    logger.error("No game found for player_id {}".format(player_id))
    return

  _, player = get_player_by_id(game, player_id)
  _, opponent = get_opponent_by_id(game, player_id)
  player['wagerAccepted'] = True
  logger.info('Current game state in on accept_wager: %s', games[game_session_id])

  if player['wagerAccepted'] and opponent['wagerAccepted']:
    logger.info('Both players accepted wagers. Generating contract.')

    tx_hash = None
    arbiter_fee_percentage = int(6.5 * 10**2) # 6.5%
    contract_owner_account = Account.from_key(CONTRACT_OWNER_PRIVATE_KEY)
    logger.info(f"Contract owner address: {contract_owner_account.address}")

    # Create contract using createContract function
    # Use your deployed factory contract address
    factory_contract_address = web3.to_checksum_address(RPS_CONTRACT_FACTORY_ADDRESS)
    # Now interact with your factory contract
    factory_contract = web3.eth.contract(address=factory_contract_address, abi=rps_contract_factory_abi)

    if 'local' in args.env:
      # running locally using ganache
      logger.info("Running locally using ganache")
      tx_hash = factory_contract.functions.createContract(arbiter_fee_percentage).transact({
        'from': web3.to_checksum_address(contract_owner_account.address)
      })
    else:
      # running on the Goerli testnet
      logger.info("Running on Goerli testnet")
      # # for Goerli, make sure to inject the poa middleware
      # web3.middleware_onion.inject(geth_poa_middleware, layer=0)
        # Set Gas Price
      gas_price = web3.eth.gas_price  # Fetch the current gas price

      # Sign transaction using the private key of the owner account
      nonce = web3.eth.get_transaction_count(contract_owner_account.address)  # Get the nonce

      # Create contract using createContract function      
      construct_txn = factory_contract.functions.createContract(
        arbiter_fee_percentage
      ).build_transaction({
        'from': web3.to_checksum_address(contract_owner_account.address),
        'nonce': nonce,
        'gas': 5000000,  # You may need to change the gas limit
        'gasPrice': math.ceil(gas_price * 1.05)
      })

      signed = contract_owner_account.sign_transaction(construct_txn)
      tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)
      
    tx_receipt = None

    # Get the transaction receipt for the contract creation transaction
    tx_receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
    logger.info(f"Transaction receipt: {tx_receipt}")
    
    # Call getContracts function
    contract_addresses = factory_contract.functions.getContracts().call()
    logging.info(f"Contract addresses: {contract_addresses}")
    # Call the getLatestContract function
    created_contract_address = factory_contract.functions.getLatestContract().call()    
    logging.info(f"Created contract address: {created_contract_address}")

    emit('wager_accepted', {
      'playerId': player['id'],
      'your_wager': player['wager'],
      'opponent_wager': opponent['wager'],
      'opponent_id': opponent['id'],
      'contractAddress': created_contract_address
    }, room=player['id'])

    emit('wager_accepted', {
      'playerId': opponent['id'],
      'your_wager': opponent['wager'],
      'opponent_wager': player['wager'],
      'opponent_id': player['id'],
      'contractAddress': created_contract_address
    }, room=opponent['id'])
  else:
    logger.info('One of the players did not accept wagers. Waiting for both players to accept wagers.')
    emit('wager_accepted', {'opponent_id': player_id}, room=opponent['id'])

@socketio.on('decline_wager')
def handle_decline_wager():
  player_id = request.sid

  game_session_id = None
  for session_id, game in games.items():
    player_role, _ = get_player_by_id(game, player_id)
    if player_role is not None:
      game_session_id = session_id
      break

  game = games[game_session_id]
  _, opponent = get_opponent_by_id(game, player_id)

  if opponent and opponent['id']:
    logger.info('Current game state in on decline_wager: %s', games[game_session_id])
    logger.info('Player declined wager. Notifying opponent.')

    emit('wager_declined', {'playerId': player_id}, room=opponent['id'])  # Notify the opponent

@socketio.on('disconnect')
def handle_disconnect():
  player_id = request.sid

  game_session_to_delete = None
  for game_session_id, game in games.items():
    player_role, player = get_player_by_id(game, player_id)
    if player_role is not None:
      _, opponent = get_opponent_by_id(game, player_id)

      if opponent and opponent['id']:
        emit('opponent_disconnected', {'playerId': player_id}, room=opponent['id'])
        leave_room(str(game_session_id))
        disconnected_players[player_id] = {
          'playerRole': player_role,
          'sessionId': game_session_id,
          'timestamp': time.time()
        }

  if game_session_to_delete is not None:
    del games[game_session_to_delete]
    logger.info('Game session {} deleted.'.format(game_session_to_delete))

  logger.info('Player {} disconnected.'.format(player_id))

@socketio.on('opponent_refresh')
def handle_opponent_refresh():
  player_id = request.sid
  for game_session_id, game in games.items():
    player_key, player = get_player_by_id(game, player_id)
    if player is not None:
      opponent_key, opponent = get_opponent_by_id(game, player_id)
      if opponent is not None:
        emit('opponent_disconnected', {'playerId': opponent['id']}, room=player_id)

      break

@socketio.on('choice')
def handle_choice(data):
  player_id = request.sid

  game_session_id = None
  for session_id, game in games.items():
    if game['player1']['id'] == player_id or game['player2']['id'] == player_id:
      game_session_id = session_id
      break

  game = games[game_session_id]
  player_key, player = get_player_by_id(game, player_id)
  opponent_key, opponent = get_opponent_by_id(game, player_id)

  player['choice'] = data['choice']
  player['wager'] = data['wager']
  logger.info('Player {} in session id {} chose: {}'.format(player_id, game_session_id, data['choice']))

  if None not in [game['player1']['choice'], game['player2']['choice']]:
    winner = determine_winner(game['player1']['choice'], game['player2']['choice'])
    set_winner(game, winner)

    logging.info('Winner: {}'.format(winner))

    if winner == 'draw':
      result = 'Draw!'
      winnings = 0
      player['winnings'] = winnings
      opponent['winnings'] = winnings
    else:
      winner_key, winner_player = get_player_by_id(game, player_id) if winner == player_key else get_opponent_by_id(game, player_id)
      loser_key, loser_player = get_opponent_by_id(game, player_id) if winner == player_key else get_player_by_id(game, player_id)

      winnings = int(loser_player['wager'])
      winner_player['winnings'] = winnings
      loser_player['winnings'] = -1 * winnings
      winner_player['totalWinnings'] += winnings
      loser_player['totalWinnings'] -= winnings

      result = 'You win!' if winner_key == player_key else 'You lose!'

    # call decideWinner contract function here
    logger.info('Calling decideWinner contract function')
    arbiter_account = Account.from_key(ARBITER_PRIVATE_KEY)
    logging.info(f"Arbiter account address: {arbiter_account.address}")
    winner_address = winner['playerAddress']
    logging.info(f"Winner account address: {winner_address}")
    rps_contract_address = web3.to_checksum_address(winner['contractAddress'])
    logging.info(f"Contract address: {rps_contract_address}")
    
    # Now interact with your factory contract
    rps_contract = web3.eth.contract(address=rps_contract_address, abi=rps_contract_abi)

    tx_hash = None
    rps_txn = None

    # Set Gas Price
    gas_price = web3.eth.gas_price  # Fetch the current gas price
    arbiter_checksum_address = web3.to_checksum_address(arbiter_account.address)

    # Sign transaction using the private key of the arbiter account
    nonce = web3.eth.get_transaction_count(arbiter_checksum_address)  # Get the nonce

    if 'local' in args.env:
      # running locally using ganache
      logger.info("Running locally using ganache")
    else:
      # running on the Goerli testnet
      logger.info("Running on Goerli testnet")
      # Let web3.py use PoA setting of the Goerli Testnet.
      web3.middleware_onion.inject(geth_poa_middleware, layer=0)

    rps_txn = rps_contract.functions.decideWinner(web3.to_checksum_address(winner_address)).build_transaction({
      'from': arbiter_checksum_address,
      'nonce': nonce,
      'gas': 800000,  # You may need to change the gas limit
      'gasPrice': math.ceil(gas_price * 1.05)
    })

    signed = arbiter_account.sign_transaction(rps_txn)
    tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)
    
    tx_receipt = None

    # Get the transaction receipt for the join contract transaction
    tx_receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
    print(f'Transaction receipt after decideWinner was called: {tx_receipt}')

    logger.info('Current game state after {} round: {}'.format(game['round'], game))

    emit('result',
    {
      'result': result,
      'your_wager': player['wager'],
      'opponent_wager': opponent['wager'],
      'yourChoice': player['choice'],
      'opponentChoice': opponent['choice'],
      'playerId': player['id'],
      'opponentId': opponent['id'],
      'winnings': player['winnings'],
      'totalWinnings': player['totalWinnings'],
      'playerAddress': player['address'],
      'contractAddress': player['contractAddress'],
      'joinContract': {
        'confirmationNumber': player['joinContract']['confirmationNumber'],
        'transactionHash': player['joinContract']['transactionHash'],
        'receipt': player['joinContract']['receipt']
      },
      'decideWinner': {
        'confirmationNumber': player['decideWinner']['confirmationNumber'],
        'transactionHash': player['decideWinner']['transactionHash'],
        'receipt': player['decideWinner']['receipt']
      }
    }, room=player['id'])

    emit('result',
    {
      'result': 'You lose!' if result == 'You win!' else 'You win!',
      'your_wager': opponent['wager'],
      'opponent_wager': player['wager'],
      'yourChoice': opponent['choice'],
      'opponentChoice': player['choice'],
      'playerId': opponent['id'],
      'opponentId': player['id'],
      'winnings': opponent['winnings'],
      'totalWinnings': opponent['totalWinnings'],
      'playerAddress': opponent['address'],
      'contractAddress': opponent['contractAddress'],
      'joinContract': {
        'confirmationNumber': opponent['joinContract']['confirmationNumber'],
        'transactionHash': opponent['joinContract']['transactionHash'],
        'receipt': opponent['joinContract']['receipt']
      },
      'decideWinner': {
        'confirmationNumber': opponent['decideWinner']['confirmationNumber'],
        'transactionHash': opponent['decideWinner']['transactionHash'],
        'receipt': opponent['decideWinner']['receipt']
      }
    }, room=opponent['id'])

if __name__ == '__main__':
  from geventwebsocket.handler import WebSocketHandler
  from gevent.pywsgi import WSGIServer

  cleanup_thread = threading.Thread(target=clean_disconnected_players)
  cleanup_thread.daemon = True
  cleanup_thread.start()

  logger.info('Starting server...')

  http_server = WSGIServer(('0.0.0.0', 443),
                           app,
                           keyfile='/etc/letsencrypt/live/dev.generalsolutions43.com/privkey.pem',
                           certfile='/etc/letsencrypt/live/dev.generalsolutions43.com/fullchain.pem',
                           handler_class=WebSocketHandler)

  # http_server = WSGIServer(('0.0.0.0', 8000), app, handler_class=WebSocketHandler)

  http_server.serve_forever()
