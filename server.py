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
  rps_contract_abi = rps_json['abi']


app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
CORS(app)
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins='*')
games = {}
player_queue = []
players = {}

def determine_winner(player1, player2):
  player1_choice = player1['choice']
  player2_choice = player2['choice']
  if player1_choice == player2_choice:
    return None, None
  elif (player1_choice == 'rock' and player2_choice == 'scissors') or \
    (player1_choice == 'paper' and player2_choice == 'rock') or \
    (player1_choice == 'scissors' and player2_choice == 'paper'):
    return player1, player2
  else:
    return player2, player1

def get_new_game():
  game_id = uuid.uuid4()
  
  new_game = {
    'id': game_id,
    'player1_id': None,
    'player2_id': None,
    'winner_id': None,
    'loser_id': None,
    'contract_address': None,
    'transactions': []
  }
  """
  transaction object
  {
    'confirmationNumber': None,
    'transactionHash': None,
    'receipt': None
  }
  """
  return new_game

def get_new_player():
  return {
    'id': None,
    'choice': None,
    'wager': None,
    'winnings': 0,
    'losses': 0,
    'wager_accepted': False,
    'wager_offered': False,
    'player_address': None
  }

@socketio.on('transaction_rejected')
def handle_transaction_rejected(data):
  the_rejector = players[data['player_address']]
  logger.info('%s decided to reject the contract.', the_rejector['id'])
  # notify the other player that the contract was rejected
  game = games[data['game_id']]
  player1_id = game['player1_id']
  player2_id = game['player2_id']

  # will need to call the contract here to refund the wager to the player that did not reject the contract

  if player1_id == the_rejector['id']:
    emit('transaction_rejected', data, room=player2_id)
  else:
    emit('transaction_rejected', data, room=player1_id)

@socketio.on('join_contract_confirmation_number_received')
def handle_join_contract_confirmation_number_received(data):
  logger.info('join contract confirmation number: %s', data)

@socketio.on('join_contract_transaction_hash_received')
def handle_join_contract_transaction(data):
  logger.info('join contract transaction hash: %s', data)
  game = games[data['game_id']]
  logger.info(f"Game: {game}")
  player = players[data['player_address']]
  logger.info(f"Player: {player}")
  game['transactions'].append(data['transaction_hash'])
  logger.info(f"Current game state in on join_contract_transaction_hash_received: {game}")

@socketio.on('join_contract_transaction_receipt_received')
def handle_join_contract_transaction(data):
  logger.info('join contract transaction receipt received: %s', data)  

@socketio.on('connect')
def handle_connect():
  params = request.args
  address = params.get('address')
  
  # remove each player in the queue that has an id equal to address
  player_queue[:] = [player for player in player_queue if player.get('id') != address]

  # create a new player
  player = get_new_player()
  player['id'] = address
  # add the player to the queue
  player_queue.append(player)
  #add the player to the players dictionary
  players['player_id'] = player

def join_game():
  # remove the first two players from the queue
  player1 = player_queue.pop(0)
  player2 = player_queue.pop(0)
  # create a new game
  game = get_new_game()
  # set the player1_id and player2_id
  game['player1_id'] = player1['id']
  game['player2_id'] = player2['id']
  # add the game to the games dictionary
  games[game['id']] = game
  # emit an event to both players to inform them that the game has started
  emit('game_started', {'game_id': game['id']}, room=player1['id'])
  emit('game_started', {'game_id': game['id']}, room=player2['id'])

@socketio.on('offer_wager')
def handle_submit_wager(data):
  logger.info('Received wager from address %s', data.player_address)
  wager = data['wager']

  # get the game session from the games dictionary
  game = games[data['game_id']]
  # get the playerId from the game session
  playerId = game['player1_id'] if data.player_address == game['player1_id'] else game['player2_id']
  # get the player from the players dictionary
  player = players[playerId]
  # assign the wager to the player
  player['wager'] = wager
  # if player1 offered the wager, emit an event to player2 to inform them that player1 offered a wager
  if data.player_address == game['player1_id']:
    emit('wager_offered', {'wager': wager}, room=game['player2_id'])
  else:
    emit('wager_offered', {'wager': wager}, room=game['player1_id'])

@socketio.on('accept_wager')
def handle_accept_wager(data):
  # get the game session from the games dictionary
  game = games[data['game_id']]
  # get the playerId from the game session
  playerId = game['player1_id'] if data['player_address'] == game['player1_id'] else game['player2_id']
  # get the player from the players dictionary
  player = players[playerId]
  # mark the player as having accepted the wager
  player['wager_accepted'] = True

  # determine if both players have accepted the wager
  player1_id = game['player1_id']
  player2_id = game['player2_id']
  player1 = players[player1_id]
  player2 = players[player2_id]

  if player1['wager_accepted'] and player2['wager_accepted']:
    logger.info('Both players have accepted a wager. Generating contract.')
    # emit an event to both players to inform them that the contract is being generated
    emit('generating_contract', {'playerId': player1_id}, room=player1_id)
    emit('generating_contract', {'playerId': player2_id}, room=player2_id)

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
    game['transactions'].append(tx_receipt)
    logger.info(f"Transaction receipt: {tx_receipt}")
    
    # Call getContracts function
    contract_addresses = factory_contract.functions.getContracts().call()
    logger.info(f"Contract addresses: {contract_addresses}")
    # Call the getLatestContract function
    created_contract_address = factory_contract.functions.getLatestContract().call()    
    logger.info(f"Created contract address: {created_contract_address}")

    # notify both players that the contract has been created
    emit('contract_created', {
      'contract_address': created_contract_address, 
      'your_wager': player1['wager'],
      'opponent_wager': player2['wager'],
    }, room=game['player1_id'])
    emit('contract_created', {
      'contract_address': created_contract_address,
      'your_wager': player2['wager'],
      'opponent_wager': player1['wager'],
    }, room=game['player2_id'])
  else:
    logger.info('Player %s accepted the wager. Waiting for opponent.', player['id'])
    # if player1_id accepted the wager, emit an event to player2 to inform them that player1 accepted the wager and visa versa
    if data['player_address'] == game['player1_id']:
      emit('wager_accepted', {'playerId': game['player1_id']}, room=game['player2_id'])
    else:
      emit('wager_accepted', {'playerId': game['player2_id']}, room=game['player1_id'])

@socketio.on('decline_wager')
def handle_decline_wager(data):
  # get the game session from the games dictionary
  game = games[data['game_id']]
  # get the playerId from the game session
  playerId = game['player1_id'] if data['player_address'] == game['player1_id'] else game['player2_id']
  # get the player from the players dictionary
  player = players[playerId]
  # mark the player as having declined the wager
  player['wager_accepted'] = False

  # emit wager declined event to the opposing player
  if data['player_address'] == game['player1_id']:
    emit('wager_declined', {'gameId': game['id']}, room=game['player2_id'])
  else:
    emit('wager_declined', {'gameId': game['id']}, room=game['player1_id'])

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

@socketio.on('choice')
def handle_choice(data):
  game = games[data['game_id']]
  # assign the choice to the player
  player = players[data['player_address']]
  player['choice'] = data['choice']
  logger.info('Player {} chose: {}'.format(data['player_address'], data['choice']))

  # if both players have chosen, determine the winner
  player1_id = game['player1_id']
  player2_id = game['player2_id']
  player1 = players[player1_id]
  player2 = players[player2_id]

  if player1['choice'] and player2['choice']:
    winner, loser = determine_winner(player1, player2)
    
    winner_address = None

    arbiter_account = Account.from_key(ARBITER_PRIVATE_KEY)
    logger.info(f"Arbiter account address: {arbiter_account.address}")
    
    if winner is None and loser is None:
      logger.info('Game is a draw.')
      # set the game winner and loser
      game['winner_id'] = None
      game['loser_id'] = None

      winner_address = arbiter_account.address    
    else:
      game['winner_id'] = winner['id']
      game['loser_id'] = loser['id']
      logger.info('Winning player: {}'.format(winner))
      logger.info('Losing player: {}'.format(loser))
      # assign winnings to the winning player
      winner['winnings'] = int(loser['wager'])
      # assign losses to the losing player
      loser['losses'] = int(loser['wager'])

      winner_address = winner['address']

    # call decideWinner contract function here
    logger.info('Calling decideWinner contract function')
    
    logger.info(f"Winner account address: {winner_address}")
    rps_contract_address = web3.to_checksum_address(game['contract_address'])
    logger.info(f"Contract address: {rps_contract_address}")
    
    # Now interact with the rps contract
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

    rps_txn = rps_contract.functions.decideWinner(web3.to_checksum_address(winner_address)).build_transaction({
      'from': arbiter_checksum_address,
      'nonce': nonce,
      'gas': 800000,  # You may need to change the gas limit
      'gasPrice': math.ceil(gas_price * 1.05)
    })

    signed = arbiter_account.sign_transaction(rps_txn)
    tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)
    
    tx_receipt = None

    # Get the transaction receipt for the decide winner transaction
    tx_receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
    print(f'Transaction receipt after decideWinner was called: {tx_receipt}')
    game['transactions'].append(tx_receipt)

    if winner is None and loser is None:
      # emit an event to both players to inform them that the game is a draw
      emit('draw', {
        'your_choice': player1['choice'],
        'opp_choice': player2['choice']}, room=player1_id)
      emit('draw', {
        'result': 'Draw!', 
        'your_choice': player2['choice'],
        'opp_choice': player1['choice']}, room=player2_id)
    else:
      # emit an event to both players to inform them of the result
      emit('you_win', {
        'your_choice': winner['choice'],
        'opp_choice': loser['choice'],
        'winnings': winner['winnings']
        }, room=winner['id'])
      emit('you_lose', {
        'your_choice': loser['choice'],
        'opp_choice': winner['choice'], 
        'losses': loser['losses']}, room=loser['id'])

if __name__ == '__main__':
  from geventwebsocket.handler import WebSocketHandler
  from gevent.pywsgi import WSGIServer

  logger.info('Starting server...')

  http_server = WSGIServer(('0.0.0.0', 443),
                           app,
                           keyfile='/etc/letsencrypt/live/dev.generalsolutions43.com/privkey.pem',
                           certfile='/etc/letsencrypt/live/dev.generalsolutions43.com/fullchain.pem',
                           handler_class=WebSocketHandler)

  # http_server = WSGIServer(('0.0.0.0', 8000), app, handler_class=WebSocketHandler)

  http_server.serve_forever()

  # run join_game every 3 seconds
  while True:
    time.sleep(3)
    if len(player_queue) >= 2:
      join_game()
      logger.info('Game started. Player queue: {}'.format(player_queue))
      logger.info('Games: {}'.format(games))
    else:
      logger.info('Waiting for players to join. Player queue: {}'.format(player_queue))
      logger.info('Games: {}'.format(games))
