import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Optional

import requests
from cchecksum import to_checksum_address
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak
from gmx_python_sdk.scripts.v2.get.get_oracle_prices import OraclePrices
from gmx_python_sdk.scripts.v2.gmx_utils import create_hash_string, get_reader_contract, get_datastore_contract
from gmx_python_sdk.scripts.v2.utils.exchange import execute_with_oracle_params
from gmx_python_sdk.scripts.v2.utils.hash_utils import hash_data
from gmx_python_sdk.scripts.v2.utils.keys import IS_ORACLE_PROVIDER_ENABLED, MAX_ORACLE_REF_PRICE_DEVIATION_FACTOR
from rich.console import Console
from web3 import HTTPProvider, Web3

from eth_defi.gmx.config import GMXConfig
from eth_defi.gmx.trading import GMXTrading
from eth_defi.hotwallet import HotWallet

# Create the ORDER_LIST key directly
ORDER_LIST = create_hash_string("ORDER_LIST")
print = Console().print

JSON_RPC_BASE = "https://virtual.arbitrum.rpc.tenderly.co/e916f78f-7d9b-4112-b6b3-1547bc2261fa"

TOKENS: dict = {
    "USDC": to_checksum_address("0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
    "SOL": to_checksum_address("0x2bcC6D6CdBbDC0a4071e48bb3B969b06B3330c07"),
    "ARB": to_checksum_address("0x912ce59144191c1204e64559fe8253a0e49e6548"),
    "LINK": to_checksum_address("0xf97f4df75117a78c1A5a0DBb814Af92458539FB4"),
}

INITIAL_TOKEN_SYMBOL: str = "ARB"
TARGET_TOKEN_SYMBOL: str = "SOL"

initial_token_address: str = TOKENS[INITIAL_TOKEN_SYMBOL]
target_token_address: str = TOKENS[TARGET_TOKEN_SYMBOL]

ABIS_PATH = os.path.dirname(os.path.abspath(__file__))


def set_opt_code(rpc, bytecode=None, contract_address=None):
    # Connect to the Anvil node
    w3 = Web3(Web3.HTTPProvider(rpc))

    # Use Anvil's RPC to set the contract's bytecode
    response = w3.provider.make_request("tenderly_setCode", [contract_address, bytecode])

    # Verify the response from Tenderly
    if response.get("result"):
        print("Code successfully set via Tenderly")
    else:
        print(f"Failed to set code: {response.get('error', {}).get('message', 'Unknown error')}")
        exit(1)

    # Now verify that the code was actually set by retrieving it
    deployed_code = w3.eth.get_code(contract_address).hex()

    # Compare the deployed code with the mock bytecode
    if deployed_code == bytecode.hex():
        print("✅ Code verification successful: Deployed bytecode matches mock bytecode")
    else:
        print("❌ Code verification failed: Deployed bytecode does not match mock bytecode")
        print(f"Expected: {bytecode.hex()}")
        print(f"Actual: {deployed_code}")

        # You can also check if the length at least matches
        if len(deployed_code) == len(bytecode) or len(deployed_code) == len("0x" + bytecode.lstrip("0x")):
            print("Lengths match but content differs")
        else:
            print(f"Length mismatch - Expected: {len(bytecode)}, Got: {len(deployed_code)}")


def execute_order(config, connection, order_key, deployed_oracle_address, logger=None, overrides=None):
    """
    Execute an order with oracle prices

    Args:
        config: Configuration object containing chain and other settings
        connection: Web3 connection object
        order_key: Key of the order to execute
        deployed_oracle_address: Address of the deployed oracle contract
        initial_token_address: Address of the initial token
        target_token_address: Address of the target token
        logger: Logger object (optional)
        overrides: Optional parameters to override defaults

    Returns:
        Result of the execute_with_oracle_params call
    """
    if logger is None:
        import logging

        logger = logging.getLogger(__name__)

    if overrides is None:
        overrides = {}

    # Process override parameters
    gas_usage_label = overrides.get("gas_usage_label")
    oracle_block_number_offset = overrides.get("oracle_block_number_offset")

    # Set token addresses if not provided
    tokens = overrides.get(
        "tokens",
        [
            initial_token_address,
            target_token_address,
        ],
    )

    # Fetch real-time prices
    oracle_prices = OraclePrices(chain=config.chain).get_recent_prices()

    # Extract prices for the tokens
    default_min_prices = []
    default_max_prices = []

    for token in tokens:
        if token in oracle_prices:
            token_data = oracle_prices[token]

            # Get the base price values
            min_price = int(token_data["minPriceFull"])
            max_price = int(token_data["maxPriceFull"])

            default_min_prices.append(min_price)
            default_max_prices.append(max_price)
        else:
            # Fallback only if token not found in oracle prices
            logger.warning(f"Price for token {token} not found, using fallback price")
            default_min_prices.append(5000 * 10**18 if token == tokens[0] else 1 * 10**9)
            default_max_prices.append(5000 * 10**18 if token == tokens[0] else 1 * 10**9)

    # Set default parameters if not provided
    data_stream_tokens = overrides.get("data_stream_tokens", [])
    data_stream_data = overrides.get("data_stream_data", [])
    price_feed_tokens = overrides.get("price_feed_tokens", [])
    precisions = overrides.get("precisions", [1, 1])

    min_prices = default_min_prices
    max_prices = default_max_prices

    # Get oracle block number if not provided
    oracle_block_number = overrides.get("oracle_block_number")
    if not oracle_block_number:
        oracle_block_number = connection.eth.block_number

    # Apply oracle block number offset if provided
    if oracle_block_number_offset:
        if oracle_block_number_offset > 0:
            # Since we can't "mine" blocks in Python directly, this would be handled differently
            # in a real application. Here we just adjust the number.
            pass

        oracle_block_number += oracle_block_number_offset

    # Extract additional oracle parameters
    oracle_blocks = overrides.get("oracle_blocks")
    min_oracle_block_numbers = overrides.get("min_oracle_block_numbers")
    max_oracle_block_numbers = overrides.get("max_oracle_block_numbers")
    oracle_timestamps = overrides.get("oracle_timestamps")
    block_hashes = overrides.get("block_hashes")

    oracle_signer = overrides.get("oracle_signer", config.get_signer())

    # Build the parameters for execute_with_oracle_params
    params = {
        "key": order_key,
        "oracleBlockNumber": oracle_block_number,
        "tokens": tokens,
        "precisions": precisions,
        "minPrices": min_prices,
        "maxPrices": max_prices,
        "simulate": overrides.get("simulate", False),
        "gasUsageLabel": gas_usage_label,
        "oracleBlocks": oracle_blocks,
        "minOracleBlockNumbers": min_oracle_block_numbers,
        "maxOracleBlockNumbers": max_oracle_block_numbers,
        "oracleTimestamps": oracle_timestamps,
        "blockHashes": block_hashes,
        "dataStreamTokens": data_stream_tokens,
        "dataStreamData": data_stream_data,
        "priceFeedTokens": price_feed_tokens,
    }

    # Create a fixture-like object with necessary properties
    fixture = {
        "config": config,
        "web3Provider": connection,
        "chain": config.chain,
        "accounts": {"signers": [oracle_signer] * 7},
        "props": {
            "oracleSalt": hash_data(["uint256", "string"], [config.chain_id, "xget-oracle-v1"]),
            "signerIndexes": [0, 1, 2, 3, 4, 5, 6],  # Default signer indexes
        },
    }

    # Call execute_with_oracle_params with the built parameters
    return execute_with_oracle_params(fixture, params, config, deployed_oracle_address=deployed_oracle_address)


GMX_ADMIN = "0x7A967D114B8676874FA2cFC1C14F3095C88418Eb"


# def add_tenderly_balances():
#     """
#     Adds both ETH and ERC20 token balances to a specific address on a Tenderly fork.
#     This function will execute both operations automatically when this script is run.
#     """
#     # Common configuration for both operations
#     tenderly_url = JSON_RPC_BASE
#     address = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
#     headers = {"Content-Type": "application/json"}
#
#     print("Starting Tenderly balance operations...")
#
#     # Step 1: Add ETH balance
#     eth_amount = "0xa3123b753e1e780000000"
#     eth_payload = {"id": 2, "jsonrpc": "2.0", "method": "tenderly_addBalance", "params": [[address], eth_amount]}
#
#     try:
#         print(f"Adding ETH balance of {eth_amount} to {address}...")
#         eth_response = requests.post(tenderly_url, headers=headers, data=json.dumps(eth_payload))
#         eth_result = eth_response.json()
#
#         if "error" in eth_result:
#             print(f"Error adding ETH balance: {eth_result['error']}")
#         else:
#             print(f"Successfully added ETH balance. Response: {eth_result}")
#     except Exception as e:
#         print(f"Exception occurred while adding ETH balance: {e}")
#
#     # Step 2: Add ERC20 token balance
#     token_address = "0x912ce59144191c1204e64559fe8253a0e49e6548"
#     token_amount = "0x1566336316561e0000000000000000"
#
#     erc20_payload = {
#         "id": 3,
#         "jsonrpc": "2.0",
#         "method": "tenderly_addErc20Balance",
#         "params": [token_address, [address], token_amount],
#     }
#
#     try:
#         print(f"Adding ERC20 balance of {token_amount} for token {token_address} to {address}...")
#         erc20_response = requests.post(tenderly_url, headers=headers, data=json.dumps(erc20_payload))
#         erc20_result = erc20_response.json()
#
#         if "error" in erc20_result:
#             print(f"Error adding ERC20 balance: {erc20_result['error']}")
#         else:
#             print(f"Successfully added ERC20 balance. Response: {erc20_result}")
#     except Exception as e:
#         print(f"Exception occurred while adding ERC20 balance: {e}")
#
#     print("Tenderly balance operations completed.")


def deploy_custom_oracle(w3: Web3, account) -> str:
    # /// Delpoy the `Oracle` contract here & then return the deployed bytecode
    # Check balance
    balance = w3.eth.get_balance(account.address)
    print(f"Deployer balance: {w3.from_wei(balance, 'ether')} ETH")

    # Load contract ABI and bytecode
    artifacts_path = Path(f"{ABIS_PATH}/mock_abis/Oracle.json")

    with open(artifacts_path) as f:
        contract_json = json.load(f)
        abi = contract_json["abi"]
        bytecode = contract_json["bytecode"]

    # Constructor arguments
    role_store = "0x3c3d99FD298f679DBC2CEcd132b4eC4d0F5e6e72"
    data_store = "0xFD70de6b91282D8017aA4E741e9Ae325CAb992d8"
    event_emitter = "0xC8ee91A54287DB53897056e12D9819156D3822Fb"
    sequender_uptime_feed = "0xFdB631F5EE196F0ed6FAa767959853A9F217697D"

    # Create contract factory
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)

    # Prepare transaction for contract deployment
    nonce = w3.eth.get_transaction_count(account.address)
    transaction = contract.constructor(role_store, data_store, event_emitter, sequender_uptime_feed).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "gas": 33000000,
            "gasPrice": w3.to_wei("50", "gwei"),
        }
    )

    # Sign transaction
    signed_txn = w3.eth.account.sign_transaction(transaction, account._private_key)

    # Send transaction
    tx_hash = w3.eth.send_raw_transaction(signed_txn.rawTransaction)
    print(f"📝 Deployment tx hash: {tx_hash.hex()}")

    # Wait for transaction receipt
    tx_receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    contract_address = tx_receipt.contractAddress
    print(f"🚀 Deployed GmOracleProvider to: {contract_address}")
    print(f"   Gas used: {tx_receipt.gasUsed}")

    # Get deployed contract
    deployed_contract = w3.eth.contract(address=contract_address, abi=abi)

    # Fetch on-chain bytecode and print its size
    code = w3.eth.get_code(contract_address)
    print(f"📦 On-chain code size (bytes): {len(code) // 2}")

    # List contract methods
    # methods = [func for func in deployed_contract.functions]
    # print("🔧 Available contract methods:")
    # for method in methods:
    #     print(f"   - {method}")

    # Verify constructor-stored state
    role_store_address = deployed_contract.functions.roleStore().call()
    data_store_address = deployed_contract.functions.dataStore().call()
    event_emitter_address = deployed_contract.functions.eventEmitter().call()

    print(f"📌 roleStore address: {role_store_address}")
    print(f"📌 dataStore address: {data_store_address}")
    print(f"📌 eventEmitter address: {event_emitter_address}")
    bytecode = w3.eth.get_code(contract_address)

    original_oracle_contract = to_checksum_address("0x918b60ba71badfada72ef3a6c6f71d0c41d4785c")

    set_opt_code(JSON_RPC_BASE, bytecode, original_oracle_contract)

    return contract_address


def deploy_custom_oracle_provider(w3: Web3, account) -> str:
    # Check balance
    balance = w3.eth.get_balance(account.address)
    print(f"Deployer balance: {w3.from_wei(balance, 'ether')} ETH")

    # Load contract ABI and bytecode
    artifacts_path = Path(f"{ABIS_PATH}/mock_abis/GmOracleProvider.json")
    with open(artifacts_path) as f:
        contract_json = json.load(f)
        abi = contract_json["abi"]
        bytecode = contract_json["bytecode"]

    # Constructor arguments
    role_store = "0x3c3d99FD298f679DBC2CEcd132b4eC4d0F5e6e72"
    data_store = "0xFD70de6b91282D8017aA4E741e9Ae325CAb992d8"
    oracle_store = "0xA8AF9B86fC47deAde1bc66B12673706615E2B011"

    # Create contract factory
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)

    # Prepare transaction for contract deployment
    nonce = w3.eth.get_transaction_count(account.address)
    transaction = contract.constructor(role_store, data_store, oracle_store).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "gas": 33000000,
            "gasPrice": w3.to_wei("50", "gwei"),
        }
    )

    # Sign transaction
    signed_txn = w3.eth.account.sign_transaction(transaction, account._private_key)

    # Send transaction
    tx_hash = w3.eth.send_raw_transaction(signed_txn.rawTransaction)
    print(f"📝 Deployment tx hash: {tx_hash.hex()}")

    # Wait for transaction receipt
    tx_receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    contract_address = tx_receipt.contractAddress
    print(f"🚀 Deployed GmOracleProvider to: {contract_address}")
    print(f"   Gas used: {tx_receipt.gasUsed}")

    # Get deployed contract
    deployed_contract = w3.eth.contract(address=contract_address, abi=abi)

    # Fetch on-chain bytecode and print its size
    code = w3.eth.get_code(contract_address)
    print(f"📦 On-chain code size (bytes): {len(code) // 2}")

    # List contract methods
    # methods = [func for func in deployed_contract.functions]
    # print("🔧 Available contract methods:")
    # for method in methods:
    #     print(f"   - {method}")

    # Verify constructor-stored state
    role_store_address = deployed_contract.functions.roleStore().call()
    data_store_address = deployed_contract.functions.dataStore().call()

    print(f"📌 roleStore address: {role_store_address}")
    print(f"📌 dataStore address: {data_store_address}")

    return contract_address


def override_storage_slot(
    contract_address: str,
    slot: str = "0x636d2c90aa7802b40e3b1937e91c5450211eefbc7d3e39192aeb14ee03e3a958",
    value: int = 171323136489203020000000,
    web3: Web3 = None,
) -> dict:
    """
    Override a storage slot in an Anvil fork.

    Args:
        contract_address: The address of the contract
        slot: The storage slot to override (as a hex string)
        value: The value to set (as an integer)
        anvil_url: URL of the Anvil node
    """

    # Check connection
    if not web3.is_connected():
        raise Exception(f"Could not connect to Anvil node at {web3.provider.endpoint_uri}")

    # Format the value to a 32-byte hex string with '0x' prefix
    # First convert to hex without '0x'
    hex_value = hex(value)[2:]

    # Pad to 64 characters (32 bytes) and add '0x' prefix
    padded_hex_value = "0x" + hex_value.zfill(64)

    # Make sure the slot has '0x' prefix
    if not slot.startswith("0x"):
        slot = "0x" + slot

    # Ensure contract address has '0x' prefix and is checksummed
    if not contract_address.startswith("0x"):
        contract_address = "0x" + contract_address

    contract_address = web3.to_checksum_address(contract_address)

    # Call the anvil_setStorageAt RPC method
    result = web3.provider.make_request("tenderly_setStorageAt", [contract_address, slot, padded_hex_value])

    # Check for errors
    if "error" in result:
        raise Exception(f"Error setting storage: {result['error']}")

    print(f"Successfully set storage at slot {slot} to {padded_hex_value}")

    storage_value = web3.eth.get_storage_at(contract_address, slot)
    print(f"Verified value: {storage_value.hex()}")

    return result


def main():
    anvil_private_key: str = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

    start_token_symbol: str = "ARB"
    out_token_symbol: str = "SOL"
    w3 = Web3(HTTPProvider(JSON_RPC_BASE))

    account = Account.from_key(anvil_private_key)
    wallet = HotWallet(account)
    wallet.sync_nonce(w3)

    config = GMXConfig(w3, chain="arbitrum", wallet=wallet, user_wallet_address=account.address)

    trading_manager = GMXTrading(config)

    erc20_abi = [
        {
            "constant": True,
            "inputs": [{"name": "_owner", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"name": "balance", "type": "uint256"}],
            "type": "function",
        },
        {
            "constant": False,
            "inputs": [
                {"name": "_to", "type": "address"},
                {"name": "_value", "type": "uint256"},
            ],
            "name": "transfer",
            "outputs": [{"name": "", "type": "bool"}],
            "type": "function",
        },
        {
            "constant": True,
            "inputs": [],
            "name": "decimals",
            "outputs": [{"name": "", "type": "uint8"}],
            "type": "function",
        },
        {
            "constant": True,
            "inputs": [],
            "name": "symbol",
            "outputs": [{"name": "", "type": "string"}],
            "payable": False,
            "stateMutability": "view",
            "type": "function",
        },
    ]

    recipient_address = account.address

    initial_token_contract = w3.eth.contract(address=initial_token_address, abi=erc20_abi)
    target_contract = w3.eth.contract(address=target_token_address, abi=erc20_abi)

    decimals = initial_token_contract.functions.decimals().call()
    symbol = initial_token_contract.functions.symbol().call()

    # Check initial balances
    balance = initial_token_contract.functions.balanceOf(recipient_address).call()
    print(f"Recipient {start_token_symbol} balance: {Decimal(balance / 10**decimals)} {symbol}")

    target_balance_before = target_contract.functions.balanceOf(recipient_address).call()
    target_symbol = target_contract.functions.symbol().call()
    target_decimals = target_contract.functions.decimals().call()

    # Convert both values to Decimal BEFORE division
    balance_decimal = Decimal(str(target_balance_before)) / Decimal(10**target_decimals)

    # Format to avoid scientific notation and show proper decimal places
    print(f"Recipient {out_token_symbol} balance before: {balance_decimal:.18f} {target_symbol}")

    swap_order = trading_manager.swap_tokens(
        out_token_symbol=out_token_symbol,
        in_token_symbol=start_token_symbol,
        amount=50000.3785643,
        slippage_percent=0.02,
        debug_mode=False,
        execution_buffer=2.5,
    )

    deployed: tuple = (None, None)  # (None, None)
    if not deployed[0]:
        deployed_oracle_address = deploy_custom_oracle_provider(w3, account)
        custom_oracle_contract_address = deploy_custom_oracle(w3, account)
    else:
        deployed_oracle_address = deployed[0]
        custom_oracle_contract_address = deployed[1]

    try:
        config = config.get_write_config()
        # order_key = order.create_order_and_get_key()

        data_store = get_datastore_contract(config)

        print(f"Order LIST: {ORDER_LIST.hex()}")

        assert ORDER_LIST.hex().removeprefix("0x") == "0x86f7cfd5d8f8404e5145c91bebb8484657420159dabd0753d6a59f3de3f7b8c1".removeprefix("0x"), "Order list mismatch"
        order_count = data_store.functions.getBytes32Count(ORDER_LIST).call()
        if order_count == 0:
            raise Exception("No orders found")

        # Get the most recent order key
        order_key = data_store.functions.getBytes32ValuesAt(ORDER_LIST, order_count - 1, order_count).call()[0]
        print(f"Order created with key: {order_key.hex()}")

        # for key in keys:
        #     print(f"Key: {key.hex()}")

        reader = get_reader_contract(config)
        order_info = reader.functions.getOrder(data_store.address, order_key).call()
        print(f"Order: {order_info}")

        # data_store_owner = "0xE7BfFf2aB721264887230037940490351700a068"
        controller = "0xf5F30B10141E1F63FC11eD772931A8294a591996"
        oracle_provider = "0x5d6B84086DA6d4B0b6C0dF7E02f8a6A039226530"
        custom_oracle_provider = deployed_oracle_address  # "0xA1D67424a5122d83831A14Fa5cB9764Aeb15CD99"
        # NOTE: Somehow have to sign the oracle params by this bad boy
        oracle_signer = "0x0F711379095f2F0a6fdD1e8Fccd6eBA0833c1F1f"
        # set this value to true to pass the provider enabled check in contract
        # OrderHandler(0xfc9bc118fddb89ff6ff720840446d73478de4153)
        data_store.functions.setBool("0x1153e082323163af55b3003076402c9f890dda21455104e09a048bf53f1ab30c", True).transact({"from": controller})

        value = data_store.functions.getBool("0x1153e082323163af55b3003076402c9f890dda21455104e09a048bf53f1ab30c").call()
        print(f"Value: {value}")

        assert value, "Value should be true"

        # * Dynamically fetch the storage slot for the oracle provider
        # ? Get this value dynamically https://github.com/gmx-io/gmx-synthetics/blob/e8344b5086f67518ca8d33e88c6be0737f6ae4a4/contracts/data/Keys.sol#L938
        # ? Python ref: https://gist.github.com/Aviksaikat/cc69acb525695e44db340d64e9889f5e
        encoded_data = encode(["bytes32", "address"], [IS_ORACLE_PROVIDER_ENABLED, custom_oracle_provider])
        slot = f"0x{keccak(encoded_data).hex()}"

        # Enable the oracle provider
        data_store.functions.setBool(slot, True).transact({"from": controller})
        is_oracle_provider_enabled: bool = data_store.functions.getBool(slot).call()
        print(f"Value: {is_oracle_provider_enabled}")
        assert is_oracle_provider_enabled, "Value should be true"

        # TODO: This will change for various tokens apparently
        # pass the test `address expectedProvider = dataStore.getAddress(Keys.oracleProviderForTokenKey(token));` in Oracle.sol#L278
        address_slot: str = "0x233a49594db4e7a962a8bd9ec7298b99d6464865065bd50d94232b61d213f16d"
        data_store.functions.setAddress(address_slot, custom_oracle_provider).transact({"from": controller})

        new_address = data_store.functions.getAddress(address_slot).call()
        print(f"New address: {new_address}")
        # 0x0000000000000000000000005d6B84086DA6d4B0b6C0dF7E02f8a6A039226530
        assert new_address == custom_oracle_provider, "New address should be the oracle provider"

        # need this to be set to pass the `Oracle._validatePrices` check. Key taken from tenderly tx debugger
        address_key: str = "0xf986b0f912da0acadea6308636145bb2af568ddd07eb6c76b880b8f341fef306"  # "0xf986b0f912da0acadea6308636145bb2af568ddd07eb6c76b880b8f341fef306"

        data_store.functions.setAddress(address_key, custom_oracle_provider).transact({"from": controller})
        value = data_store.functions.getAddress(address_key).call()
        print(f"Value: {value}")
        assert value == custom_oracle_provider, "Value should be recipient address"

        # ? Set another key value to pass the test in `Oracle.sol` this time for ChainlinkDataStreamProvider
        address_key: str = "0x659d3e479f4f2d295ea225e3d439a6b9d6fbf14a5cd4689e7d007fbab44acb8a"
        data_store.functions.setAddress(address_key, custom_oracle_provider).transact({"from": controller})
        value = data_store.functions.getAddress(address_key).call()
        print(f"Value: {value}")
        assert value == custom_oracle_provider, "Value should be recipient address"

        # ? Set the `maxRefPriceDeviationFactor` to pass tests in `Oracle.sol`
        price_deviation_factor_key: str = f"0x{MAX_ORACLE_REF_PRICE_DEVIATION_FACTOR.hex()}"
        # * set some big value to pass the test
        large_value: int = 10021573904618365809021423188717
        data_store.functions.setUint(price_deviation_factor_key, large_value).transact({"from": controller})
        value = data_store.functions.getUint(price_deviation_factor_key).call()
        print(f"Value: {value}")
        assert value == large_value, f"Value should be {large_value}"

        oracle_contract: str = "0x918b60ba71badfada72ef3a6c6f71d0c41d4785c"
        token_b_max_value_slot: str = "0x636d2c90aa7802b40e3b1937e91c5450211eefbc7d3e39192aeb14ee03e3a958"
        token_b_min_value_slot: str = "0x636d2c90aa7802b40e3b1937e91c5450211eefbc7d3e39192aeb14ee03e3a959"

        oracle_prices = OraclePrices(chain=config.chain).get_recent_prices()

        max_price: int = int(oracle_prices[TOKENS[TARGET_TOKEN_SYMBOL]]["maxPriceFull"])
        min_price: int = int(oracle_prices[TOKENS[TARGET_TOKEN_SYMBOL]]["minPriceFull"])
        max_res = override_storage_slot(oracle_contract, token_b_max_value_slot, max_price, w3)
        min_res = override_storage_slot(oracle_contract, token_b_min_value_slot, min_price, w3)

        print(f"Max price: {max_price}")
        print(f"Min price: {min_price}")
        print(f"Max res: {max_res}")
        print(f"Min res: {min_res}")

        # # ! Can't do it here
        # oracle_contract = get_contract_object(config.get_web3_connection(), "oracle", config.chain)
        # # ETH PRICE
        # oracle_contract.functions.setPrimaryPrice(link_token_address, (2492652716024169, 2492891019455477)).transact({"from": controller})

        # print(f"Order key: {order_key.hex()}")
        overrides = {
            "simulate": False,
            # "oracle_signer": oracle_signer,
            # "priceFeedTokens": [
            #     to_checksum_address("0xf97f4df75117a78c1a5a0dbb814af92458539fb4"),  # LINK on Arbitrum
            #     to_checksum_address("0x2bcC6D6CdBbDC0a4071e48bb3B969b06B3330c07"),  # SOL on Arbitrum
            # ],
        }
        # Execute the order with oracle prices
        execute_order(
            config=config,
            connection=w3,
            order_key=order_key,
            deployed_oracle_address=deployed_oracle_address,
            overrides=overrides,
        )

        # Check the balances after execution
        balance = initial_token_contract.functions.balanceOf(recipient_address).call()
        symbol = initial_token_contract.functions.symbol().call()
        print(f"Recipient {INITIAL_TOKEN_SYMBOL} balance after swap: {Decimal(balance / 10**decimals)} {symbol}")

        target_balance_after = target_contract.functions.balanceOf(recipient_address).call()
        symbol = target_contract.functions.symbol().call()
        target_decimals = target_contract.functions.decimals().call()

        balance_decimal = Decimal(str(target_balance_after)) / Decimal(10**target_decimals)

        # Format to avoid scientific notation and show proper decimal places
        print(f"Recipient {TARGET_TOKEN_SYMBOL} balance after swap: {balance_decimal:.18f} {target_symbol}")
        print(f"Change in {TARGET_TOKEN_SYMBOL} balance: {Decimal((target_balance_after - target_balance_before) / 10**target_decimals):.18f}")
    except Exception as e:
        print(f"Error during swap process: {e!s}")
        raise e


if __name__ == "__main__":
    main()
