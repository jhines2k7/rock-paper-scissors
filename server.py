import datetime
import sys
import logging
import uuid
import json
import argparse
import os
import math
import queue

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account
from googleapiclient.http import MediaFileUpload
from googleapiclient.http import MediaIoBaseDownload
from gevent import monkey
monkey.patch_all()

from flask import Flask, jsonify, request
from flask_socketio import SocketIO, join_room, emit, leave_room
from flask_cors import CORS
from web3 import Web3
from web3.middleware import geth_poa_middleware
from eth_account import Account
from dotenv import load_dotenv

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
ARBITER_ADDRESS = os.getenv("ARBITER_ADDRESS")
logger.info(f"Arbiter address: {ARBITER_ADDRESS}")
RPS_CONTRACT_FACTORY_ADDRESS = os.getenv("RPS_CONTRACT_FACTORY_ADDRESS")
# Connection
web3 = Web3(Web3.HTTPProvider(HTTP_PROVIDER))

if 'test' in args.env:
  logger.info("Running on testnet")
  # for Goerli, make sure to inject the poa middleware
  web3.middleware_onion.inject(geth_poa_middleware, layer=0)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
CORS(app)
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins='*')
games = {}
player_queue = queue.Queue()
players = {}

rps_contract_factory_abi = None
rps_contract_abi = None

def get_service(api_name, api_version, scopes, key_file_location):
  """Get a service that communicates to a Google API.

  Args:
    api_name: The name of the api to connect to.
    api_version: The api version to connect to.
    scopes: A list auth scopes to authorize for the application.
    key_file_location: The path to a valid service account JSON key file.

  Returns:
    A service that is connected to the specified API.
  """

  credentials = service_account.Credentials.from_service_account_file(
  key_file_location)

  scoped_credentials = credentials.with_scopes(scopes)

  # Build the service object.
  service = build(api_name, api_version, credentials=scoped_credentials)

  return service

def download_contract_abi():
  # Define the auth scopes to request.
  scope = 'https://www.googleapis.com/auth/drive.file'
  key_file_location = 'service-account.json'
    
  # Specify the name of the folder you want to retrieve
  folder_name = 'rock-paper-scissors'
  
  try:
    # Authenticate and construct service.
    service = get_service(
      api_name='drive',
      api_version='v3',
      scopes=[scope],
      key_file_location=key_file_location)
        
    results = service.files().list(q=f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder'").execute()
    folders = results.get('files', [])
    folder_id = None

    # Print the folder's ID if found
    if len(folders) > 0:
      logger.info(f"Folder ID: {folders[0]['id']}")
      logger.info(f"Folder name: {folders[0]['name']}")
      folder_id = folders[0]['id']
    else:
      logger.info("Folder not found.")

    # Delete all files in the local contracts folder
    download_dir = 'contracts'
    logger.info('Deleting all files in the contracts folder...')
    for file in os.listdir(download_dir):
      file_path = os.path.join(download_dir, file)
      try:
        if os.path.isfile(file_path):
          os.unlink(file_path)
      except Exception as e:
        logger.error(f"An error occurred while deleting file: {file_path}")
        logger.error(e)
    # Getting all files in the contracts folder
    results = service.files().list(q=f"'{folder_id}' in parents and trashed=false", pageSize=1000, fields="nextPageToken, files(id, name, createdTime)").execute()
    files = results.get('files', [])
    
    logger.info('Downloading contract ABIs...')
    # Download each file from the folder
    for file in files:
      request_file = service.files().get_media(fileId=file['id'])
      # Get the file metadata
      file_metadata = service.files().get(fileId=file['id']).execute()
      file_name = file_metadata['name']
      created_time = datetime.datetime.strptime(file['createdTime'], "%Y-%m-%dT%H:%M:%S.%fZ")
      logger.info(f"File Name: {file_name}, Created Time: {created_time}")

      # Download the file content
      fh = open(os.path.join("contracts", file_name), 'wb')
      downloader = MediaIoBaseDownload(fh, request_file)

      done = False
      while done is False:
        status, done = downloader.next_chunk()
        logger.info(f"Download progress {int(status.progress() * 100)}%.")

      print('File downloaded successfully.')

    logger.info('Contract ABIs downloaded successfully!')

    # Load and parse the contract ABIs.
    with open('contracts/RPSContractFactory.json') as f:
      global rps_contract_factory_abi
      factory_json = json.load(f)
      rps_contract_factory_abi = factory_json['abi']
    with open('contracts/RPSContract.json') as f:
      global rps_contract_abi
      rps_json = json.load(f)
      rps_contract_abi = rps_json['abi']

  except HttpError as error:
    # TODO(developer) - Handle errors from drive API.
    logger.error(f'An error occurred: {error}')

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
  new_game = {
    'id': str(uuid.uuid4()),
    'player1': None,
    'player2': None,
    'winner': None,
    'loser': None,
    'contract_address': None,
    'transactions': [],
    'join_contract_transaction_rejected': False,
    'game_over': False
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
    'game_id': None,
    'address': None,
    'choice': None,
    'wager': None,
    'winnings': 0,
    'losses': 0,
    'wager_accepted': False,
    'wager_offered': False
  }

@app.route('/rps-contract-abi', methods=['GET'])
def get_rps_contract_abi():
  with open('contracts/RPSContract.json') as f:
    return json.load(f)

@socketio.on('join_contract_transaction_rejected')
def handle_transaction_rejected(data):
  game = games[data['game_id']]

  if game['join_contract_transaction_rejected']:
    return
  
  game['join_contract_transaction_rejected'] = True
  payee = None
  payee_address = None
  # determine which player rejected the contract
  # notify the other player that the contract was rejected
  if game['player1']['address'] == data['address']:
    payee = game['player2']
    payee_address = game['player2']['address']
  else:
    payee = game['player1']
    payee_address = game['player1']['address']

  # will need to call the contract to refund the player that did not reject the contract

  logger.info('%s decided to reject the contract.', data['address'])
  arbiter_account = Account.from_key(ARBITER_PRIVATE_KEY)
  logger.info(f"Arbiter account address: {arbiter_account.address}")

  # call refundWager contract function here
  logger.info('Calling refundWager contract function')
  
  logger.info(f"Payee account address: {payee['address']}")
  rps_contract_address = web3.to_checksum_address(game['contract_address'])
  logger.info(f"Contract address: {rps_contract_address}")
  
  # Now interact with the rps contract
  global rps_contract_abi
  rps_contract = web3.eth.contract(address=rps_contract_address, abi=rps_contract_abi)

  tx_hash = None
  rps_txn = None

  # Set Gas Price
  gas_price = web3.eth.gas_price  # Fetch the current gas price
  arbiter_checksum_address = web3.to_checksum_address(arbiter_account.address)

  # Sign transaction using the private key of the arbiter account
  nonce = web3.eth.get_transaction_count(arbiter_checksum_address)  # Get the nonce

  if 'ganache' in args.env:
    # running on a ganache test network
    logger.info("Running on a ganache test network")
  else:
    # running on the Goerli testnet
    logger.info("Running on Goerli testnet")

  rps_txn = rps_contract.functions.refundWager(web3.to_checksum_address(payee_address)).build_transaction({
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
  print(f'Transaction receipt after refundWager was called: {tx_receipt}')
  game['transactions'].append(tx_receipt)

  emit('join_contract_transaction_rejected', data, room=payee['address'])

@socketio.on('join_contract_confirmation_number_received')
def handle_join_contract_confirmation_number_received(data):
  logger.info('join contract confirmation number: %s', data)

@socketio.on('join_contract_transaction_hash_received')
def handle_join_contract_transaction(data):
  logger.info('join contract transaction hash: %s', data)
  game = games[data['game_id']]
  logger.info(f"Game: {game}")
  game['transactions'].append(data['transaction_hash'])
  logger.info(f"Current game state in on join_contract_transaction_hash_received: {game}")

@socketio.on('join_contract_transaction_receipt_received')
def handle_join_contract_transaction(data):
  logger.info('join contract transaction receipt received: %s', data)  

@socketio.on('connect')
def handle_connect():
  params = request.args
  address = params.get('address')
  
  # create a new player
  player = get_new_player()
  player['address'] = address
  # add the player to the queue
  logger.info('Adding player with address {} to queue'.format(address))
  player_queue.put(player)

  join_room(address)
  
  if player_queue.qsize() >= 2:
    join_game()
    logger.info('Game started. Number of players in queue: {}'.format(player_queue.qsize()))
    logger.info('Games: {}'.format(games))
  else:
    logger.info('Waiting for players to join. Number of players in queue: {}'.format(player_queue.qsize()))
    logger.info('Games: {}'.format(games))

def join_game():
  # try to remove the first two players from the queue
  player1 = None
  player2 = None

  try:
    player1 = player_queue.get_nowait()
  except queue.Empty:
    logger.info('Player queue is empty.')
    return
  try:
    player2 = player_queue.get_nowait()
  except queue.Empty:
    # put the first player back in the queue
    player_queue.put(player1)
    logger.info('Player queue has less than two players.')
    return
  # if both players have the same address, return
  if player1['address'] == player2['address']:
      logger.info('Both players have the same address.')
      return
  
  # determine if either player has already joined a game
  for game in games.values():
    if game['player1']['address'] == player1['address'] or \
      game['player2']['address'] == player1['address']:
      logger.info('Player {} has already joined a game.'.format(player1['address']))
      return
    elif game['player1']['address'] == player2['address'] or \
      game['player2']['address'] == player2['address']:
      logger.info('Player {} has already joined a game.'.format(player2['address']))
      return
  
  # create a new game
  game = get_new_game()
  # set the game_id for each player
  player1['game_id'] = game['id']
  player2['game_id'] = game['id']
  # set the player1 and player2
  game['player1'] = player1
  game['player2'] = player2
  # add the game to the games dictionary
  games[game['id']] = game

  # emit an event to both players to inform them that the game has started
  emit('game_started', {'game_id': str(game['id'])}, room=game['player1']['address'])
  emit('game_started', {'game_id': str(game['id'])}, room=game['player2']['address'])
  
@socketio.on('offer_wager')
def handle_submit_wager(data):
  address = data['address']
  logger.info('Received wager from address %s', address)
  wager = data['wager']

  # get the game session from the games dictionary
  game = games[data['game_id']]
  # exit early if the game is over
  if game['game_over']:
    logger.info('Game is over.')
    # log the address of the player that tried to submit a wager
    logger.info('Player %s tried to submit a wager.', address)
    return
  # assign the wager to the correct player  
  # if player1 offered the wager, emit an event to player2 to inform them that player1 offered a wager
  if address == game['player1']['address']:
    game['player1']['wager'] = wager
    emit('wager_offered', {'wager': wager}, room=game['player2']['address'])
  else:
    game['player2']['wager'] = wager
    emit('wager_offered', {'wager': wager}, room=game['player1']['address'])

@socketio.on('accept_wager')
def handle_accept_wager(data):
  address = data['address']
  # get the game session from the games dictionary
  game = games[data['game_id']]
  # exit early if the game is over
  if game['game_over']:
    logger.info('Game is over.')
    # log the address of the player that tried to submit a wager
    logger.info('Player %s tried to submit a wager.', address)
    return
  # mark the player as having accepted the wager
  if address == game['player1']['address']:
    game['player1']['wager_accepted'] = True
    emit('wager_accepted', room=game['player2']['address'])
  else:
    game['player2']['wager_accepted'] = True
    emit('wager_accepted', room=game['player1']['address'])

  logger.info('Player %s accepted the wager. Waiting for opponent.', address)

  if game['player1']['wager_accepted'] and game['player2']['wager_accepted']:
    logger.info('Both players have accepted a wager. Generating contract.')
    # emit an event to both players to inform them that the contract is being generated
    emit('generating_contract', room=game['player1']['address'])
    emit('generating_contract', room=game['player2']['address'])

    tx_hash = None
    arbiter_fee_percentage = int(6.5 * 10**2) # 6.5%
    contract_owner_account = Account.from_key(CONTRACT_OWNER_PRIVATE_KEY)
    logger.info(f"Contract owner address: {contract_owner_account.address}")

    # Create contract using createContract function
    # Use your deployed factory contract address
    factory_contract_address = web3.to_checksum_address(RPS_CONTRACT_FACTORY_ADDRESS)
    # Now interact with your factory contract
    global rps_contract_factory_abi
    factory_contract = web3.eth.contract(address=factory_contract_address, abi=rps_contract_factory_abi)

    game_id = game['id']
    if 'ganache' in args.env:
      # running on a ganache test network
      logger.info("Running on a ganache test network")
      tx_hash = factory_contract.functions.createContract(arbiter_fee_percentage, game_id).transact({
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
    contract_addresses = factory_contract.functions.getContracts().call({
      "from": web3.to_checksum_address(contract_owner_account.address)
    })
    logger.info(f"Contract addresses: {contract_addresses}")
    # Call the getLatestContract function
    created_contract_address = factory_contract.functions.getLatestContract().call({
      "from": web3.to_checksum_address(contract_owner_account.address)
    })  
    logger.info(f"Created contract address: {created_contract_address}")
    game['contract_address'] = created_contract_address

    # set arbiter for the contract
    rps_contract_address = web3.to_checksum_address(created_contract_address)
    logger.info(f"Contract address: {rps_contract_address}")
    
    # Now interact with the rps contract
    global rps_contract_abi
    rps_contract = web3.eth.contract(address=rps_contract_address, abi=rps_contract_abi)

    tx_hash = None
    set_arbiter_txn = None

    # Set Gas Price
    gas_price = web3.eth.gas_price  # Fetch the current gas price
    contract_owner_checksum_address = web3.to_checksum_address(contract_owner_account.address)

    # Sign transaction using the private key of the contract owner account
    nonce = web3.eth.get_transaction_count(contract_owner_checksum_address)  # Get the nonce

    # call setArbiter
    logger.info('Calling setArbiter contract function')
    set_arbiter_txn = rps_contract.functions.setArbiter(web3.to_checksum_address(ARBITER_ADDRESS)).build_transaction({
      'from': contract_owner_checksum_address,
      'nonce': nonce,
      'gas': 800000,  # You may need to change the gas limit
      'gasPrice': math.ceil(gas_price * 1.05)
    })

    signed = contract_owner_account.sign_transaction(set_arbiter_txn)
    tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)

    # Get the transaction receipt for the contract creation transaction
    tx_receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
    game['transactions'].append(tx_receipt)
    logger.info(f"Transaction receipt: {tx_receipt}")

    # notify both players that the contract has been created
    emit('contract_created', {
      'contract_address': created_contract_address, 
      'your_wager': game['player1']['wager'],
      'opponent_wager': game['player2']['wager'],
    }, room=game['player1']['address'])
    emit('contract_created', {
      'contract_address': created_contract_address,
      'your_wager': game['player2']['wager'],
      'opponent_wager': game['player1']['wager'],
    }, room=game['player2']['address'])


@socketio.on('decline_wager')
def handle_decline_wager(data):
  address = data['address']
  # get the game session from the games dictionary
  game = games[data['game_id']]
  # exit early if the game is over
  if game['game_over']:
    logger.info('Game is over.')
    # log the address of the player that tried to submit a wager
    logger.info('Player %s tried to submit a wager.', address)
    return
  # mark the player as having declined the wager
  if game['player1']['address'] == address:
    game['player1']['wager_accepted'] = False
    # emit wager declined event to the opposing player
    emit('wager_declined', {'game_id': data['game_id']}, room=game['player2']['address'])
  else:
    game['player2']['wager_accepted'] = False
    # emit wager declined event to the opposing player
    emit('wager_declined', {'game_id': data['game_id']}, room=game['player1']['address'])

@socketio.on('choice')
def handle_choice(data):
  address = data['address']
  game = games[data['game_id']]
  # exit early if the game is over
  if game['game_over']:
    logger.info('Game is over.')
    # log the address of the player that tried to submit a wager
    logger.info('Player %s tried to submit a wager.', address)
    return
  # assign the choice to the player
  if game['player1']['address'] == address:
    game['player1']['choice'] = data['choice']
  else:
    game['player2']['choice'] = data['choice']

  logger.info('Player {} chose: {}'.format(data['address'], data['choice']))

  if game['player1']['choice'] and game['player2']['choice']:
    winner, loser = determine_winner(game['player1'], game['player2'])
    winner_address = None

    contract_owner_account = Account.from_key(CONTRACT_OWNER_PRIVATE_KEY)
    logger.info(f"Contract owner account address: {contract_owner_account.address}")
    
    if winner is None and loser is None:
      logger.info('Game is a draw.')
      # set the game winner and loser
      game['winner'] = None
      game['loser'] = None

      winner_address = ARBITER_ADDRESS
    else:
      game['winner'] = winner
      game['loser'] = loser
      logger.info('Winning player: {}'.format(winner))
      logger.info('Losing player: {}'.format(loser))
      # assign winnings to the winning player
      # losing_wager = int(loser['wager'].replace('$', ''))
      game['winner']['winnings'] = loser['wager']
      # assign losses to the losing player
      game['loser']['losses'] = loser['wager']

      winner_address = winner['address']

    # call decideWinner contract function here
    logger.info('Calling decideWinner contract function')
    
    logger.info(f"Winner account address: {winner_address}")
    rps_contract_address = web3.to_checksum_address(game['contract_address'])
    logger.info(f"Contract address: {rps_contract_address}")
    
    # Now interact with the rps contract
    global rps_contract_abi
    rps_contract = web3.eth.contract(address=rps_contract_address, abi=rps_contract_abi)

    tx_hash = None
    rps_txn = None
    tx_receipt = None

    # Set Gas Price
    gas_price = web3.eth.gas_price  # Fetch the current gas price
    contract_owner_checksum_address = web3.to_checksum_address(contract_owner_account.address)

    # Sign transaction using the private key of the contract owner account
    nonce = web3.eth.get_transaction_count(contract_owner_checksum_address)  # Get the nonce

    if 'ganache' in args.env:
      # running on a ganache test network
      logger.info("Running on a ganache test network")
    else:
      # running on the Goerli testnet
      logger.info("Running on Goerli testnet")
    
    rps_txn = rps_contract.functions.decideWinner(web3.to_checksum_address(winner_address)).build_transaction({
      'from': contract_owner_checksum_address,
      'nonce': nonce,
      'gas': 800000,  # You may need to change the gas limit
      'gasPrice': math.ceil(gas_price * 1.05)
    })

    signed = contract_owner_account.sign_transaction(rps_txn)
    tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)
    
    # Get the transaction receipt for the decide winner transaction
    tx_receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
    logger.info(f'Transaction receipt after decideWinner was called: {tx_receipt}')
    game['transactions'].append(tx_receipt)

    if winner is None and loser is None:
      # emit an event to both players to inform them that the game is a draw
      emit('draw', {
        'your_choice': game['player1']['choice'],
        'opp_choice': game['player2']['choice']}, room=game['player1']['address'])
      emit('draw', {
        'your_choice': game['player2']['choice'],
        'opp_choice': game['player1']['choice']}, room=game['player2']['address'])
    else:
      # emit an event to both players to inform them of the result
      emit('you_win', {
        'your_choice': game['winner']['choice'],
        'opp_choice': game['loser']['choice'],
        'winnings': game['winner']['winnings']
        }, room=game['winner']['address'])
      emit('you_lose', {
        'your_choice': game['loser']['choice'],
        'opp_choice': game['winner']['choice'], 
        'losses': game['loser']['losses']
        }, room=game['loser']['address'])
      
  # GAME OVER, man!
  game['game_over'] = True

@socketio.on('disconnect')
def handle_disconnect():
  # the `request` context still contains the disconnection information
  address = request.args.get('address')
  logger.info('Player with address {} disconnected.'.format(address))
  # find the player in the games dictionary
  for game in games.values():
    if game['player1']['address'] == address:
      logger.info('Player1 {} disconnected from game {}.'.format(address, game['id']))
      # emit an event to the other player to inform them that the player disconnected
      emit('opponent_disconnected', room=game['player2']['address'])
      # remove the game from the games dictionary
      del games[game['id']]
      break
    elif game['player2']['address'] == address:
      logger.info('Player1 {} disconnected from game {}.'.format(address, game['id']))
      # emit an event to the other player to inform them that the player disconnected
      emit('opponent_disconnected', room=game['player1']['address'])
      # remove the game from the games dictionary
      del games[game['id']]
      break
 
if __name__ == '__main__':
  from geventwebsocket.handler import WebSocketHandler
  from gevent.pywsgi import WSGIServer
  
  logger.info('Downloading contract ABIs...')
  download_contract_abi()

  logger.info('Starting server...')

  http_server = WSGIServer(('0.0.0.0', 443),
                           app,
                           keyfile='/etc/letsencrypt/live/dev.generalsolutions43.com/privkey.pem',
                           certfile='/etc/letsencrypt/live/dev.generalsolutions43.com/fullchain.pem',
                           handler_class=WebSocketHandler)

  # http_server = WSGIServer(('0.0.0.0', 8000), app, handler_class=WebSocketHandler)

  http_server.serve_forever()