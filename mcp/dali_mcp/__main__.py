import argparse
import json
import functools
import inspect
from . import __version__
from typing import Any, Literal
from pathlib import Path
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import StrictInt, StrictFloat, StrictBool
from .authoring import PercentChange
from .service import Service, SceneChange, LiveChange
from .jobs import JobManager
from .group_execution import ExperimentalGroupController
from .parameter_review import ParameterReviewer, ParameterReviewTarget
from .fade_control import FadeTimeController
from .parameter_control import LampParameterController
from .progress import run_with_progress
from .blink import BlinkController
from .native_file import NativeFileController
from .native_floorplan_file import NativeFloorplanFileController
from .native_workbook_store import NativeWorkbookStore
from .native_workbook_scenes import NativeWorkbookScenes
from .native_workbook_groups import NativeWorkbookGroups
from .native_workbook_parameters import NativeWorkbookParameters
from .parameter_batch import ParameterBatchController
from .native_workbook_names import NativeWorkbookNames
from .project_restore import ProjectRestoreReviewer
from .project_execution import ProjectRestoreController
from .address_moves import AddressMoveController
from .address_removal import AddressRemovalController
from .address_allocation import AddressAllocationController
from .address_metadata import AddressMetadataController
from .gateway_discovery import windows_interfaces, search_gateways, verify_connection
from .session_gate import SessionGate
import time, uuid
from .project_names import ProjectNames
from .project_delivery import ProjectDeliveryController

def build_runtime(config,state_dir):
    service = Service(config, state_dir)
    jobs = JobManager(service)
    groups = ExperimentalGroupController(service)
    parameters = ParameterReviewer(service)
    fades = FadeTimeController(service)
    lamp_parameters = LampParameterController(service)
    blinks = BlinkController(service)
    native_files = NativeFileController(service)
    native_floorplan_files = NativeFloorplanFileController(service)
    native_workbooks = NativeWorkbookStore(service)
    native_scenes = NativeWorkbookScenes(service)
    native_groups = NativeWorkbookGroups(service)
    native_parameters = NativeWorkbookParameters(service)
    parameter_batches = ParameterBatchController(service)
    native_workbook_names = NativeWorkbookNames(service)
    project_restore = ProjectRestoreReviewer(service)
    project_execution = ProjectRestoreController(service)
    address_moves = AddressMoveController(service)
    address_removal = AddressRemovalController(service)
    address_allocation = AddressAllocationController(service)
    address_metadata = AddressMetadataController(service)
    project_names = ProjectNames(service)
    project_delivery = ProjectDeliveryController(service)
    return service,jobs,groups,parameters,fades,lamp_parameters,blinks,native_files,native_floorplan_files,native_workbooks,native_scenes,native_groups,native_parameters,parameter_batches,native_workbook_names,project_restore,project_execution,address_moves,address_removal,address_allocation,address_metadata,project_names,project_delivery

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="dali-connection/config.json")
    args = parser.parse_args()
    path = Path(args.config).resolve()
    initial_config=json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    selected=all(initial_config.get(k) for k in ("gateway_ip","local_ip","expected_dali_mac","expected_firmware"))
    gate=SessionGate(); searches={}
    known_state_dirs={initial_config["expected_dali_mac"].lower():path.parent/"state"} if selected else {}
    service,jobs,groups,parameters,fades,lamp_parameters,blinks,native_files,native_floorplan_files,native_workbooks,native_scenes,native_groups,native_parameters,parameter_batches,native_workbook_names,project_restore,project_execution,address_moves,address_removal,address_allocation,address_metadata,project_names,project_delivery=build_runtime(initial_config,path.parent/"state")
    server = MCPServer("DALI Config Tool Gateway", version=__version__)
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

    def call_scope(name):
        if name in ("dali_search_gateways","dali_select_gateway"):
            return gate.selection([jobs,groups,parameters,fades,lamp_parameters,blinks,native_files,native_floorplan_files,native_workbooks,native_scenes,native_groups,native_parameters,parameter_batches,native_workbook_names,project_restore,project_execution,address_moves,address_removal,address_allocation,address_metadata,project_names,project_delivery])
        if not selected and name not in ("dali_status","dali_list_network_interfaces","dali_get_gateway_selection"):
            raise RuntimeError("No gateway selected; search then select a gateway first")
        return gate.operation()

    def expected_errors(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapped(*args, **kwargs):
                try:
                    with call_scope(fn.__name__):
                        return await fn(*args, **kwargs)
                except (ValueError, OSError, RuntimeError) as exc:
                    raise ToolError(str(exc)) from exc
            return async_wrapped
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                with call_scope(fn.__name__):
                    return fn(*args, **kwargs)
            except (ValueError, OSError, RuntimeError) as exc:
                raise ToolError(str(exc)) from exc
        return wrapped

    @server.tool(annotations=read, structured_output=True)
    @expected_errors
    def dali_list_network_interfaces() -> dict[str, Any]:
        """List operational Windows IPv4 network interfaces; no gateway traffic."""
        return dict(interfaces=windows_interfaces(),network_traffic=False)

    @server.tool(annotations=read, structured_output=True)
    @expected_errors
    def dali_search_gateways(local_ips:list[str]|None=None,timeout_seconds:StrictFloat|StrictInt=2.0)->dict[str,Any]:
        """Broadcast FE01 gateway search on local IPv4 interfaces without a preconfigured gateway IP. Omit local_ips to search all operational broadcast-capable NICs; specify NIC IPs to narrow. Lists all replies; never selects/controls a lamp. Finite per-NIC window; no cross-router discovery guarantee. Busy tasks prevent search. Results expire after5minutes."""
        interfaces=windows_interfaces()
        if local_ips is not None:
            if not local_ips or len(set(local_ips))!=len(local_ips):raise ValueError('nonempty unique NIC addresses required')
            if set(local_ips)-{x['local_ip'] for x in interfaces}:raise ValueError('selected NIC no longer available')
            interfaces=[x for x in interfaces if x['local_ip'] in local_ips]
        result=search_gateways(interfaces,timeout_seconds)
        ident=uuid.uuid4().hex;expires=time.time()+300
        result.update(search_id=ident,expires_at=expires)
        for index,row in enumerate(result['gateways']):row['candidate_id']=str(index)
        searches[ident]=result
        for key in list(searches):
            if searches[key]['expires_at']<time.time():del searches[key]
        while len(searches)>20:del searches[next(iter(searches))]
        return result

    @server.tool(annotations=read, structured_output=True)
    @expected_errors
    def dali_get_gateway_selection()->dict[str,Any]:
        """Return locally selected target/profile; no network traffic or online claim."""
        return dict(selected=selected,network_traffic=False,
                    gateway={k:service.config.get(k) for k in ('gateway_ip','local_ip','expected_dali_mac','expected_firmware')} if selected else None,
                    state_dir=str(service.state_dir) if selected else None)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_select_gateway(search_id:str,candidate_id:str)->dict[str,Any]:
        """Explicitly select a fresh search result, verify identity by unicast and perform handshake. Does not change gateway settings or lamps. Only validated firmware1.6.9 may activate control. Switch rejected during calls/background jobs. Each gateway MAC has isolated state; connection switches close all stored-write gates and never import another project's native cache binding. Selection stays in this server session; result records profile path for next startup."""
        nonlocal service,jobs,groups,parameters,fades,lamp_parameters,blinks,native_files,native_floorplan_files,native_workbooks,native_scenes,native_groups,native_parameters,parameter_batches,native_workbook_names,project_restore,project_execution,address_moves,address_removal,address_allocation,address_metadata,project_names,project_delivery,selected
        result=searches.get(search_id)
        if not result or result['expires_at']<time.time():raise ValueError('search expired or unknown; search again')
        candidates=[x for x in result['gateways'] if x['candidate_id']==candidate_id]
        if len(candidates)!=1:raise ValueError('candidate not found in this search')
        candidate=candidates[0]
        if candidate['dali_firmware']!='1.6.9':raise ValueError('gateway discovered but firmware is not validated for control')
        connection=verify_connection(candidate)
        config=dict(gateway_ip=candidate['ip'],local_ip=candidate['local_ip'],listen_port=19502,
                    timeout_seconds=4,expected_dali_mac=candidate['dali_mac'],expected_firmware=candidate['dali_firmware'])
        state=known_state_dirs.get(candidate['dali_mac'].lower(),path.parent/'gateways'/candidate['dali_mac'].replace(':','')/'state')
        profile=state.parent
        profile.mkdir(parents=True,exist_ok=True)
        record=profile/('connection-'+uuid.uuid4().hex+'.json')
        # Persist before changing the live target; persistence failure keeps old target.
        with record.open('x',encoding='utf-8') as output:json.dump(config,output,indent=2)
        if state.resolve()==service.state_dir.resolve():
            # Same profile, new IP/NIC: retain controller journals; remove all prior gates.
            service.config.clear();service.config.update(config)
        else:
            new_runtime=build_runtime(config,state)
            jobs.owner.close()
            service,jobs,groups,parameters,fades,lamp_parameters,blinks,native_files,native_floorplan_files,native_workbooks,native_scenes,native_groups,native_parameters,parameter_batches,native_workbook_names,project_restore,project_execution,address_moves,address_removal,address_allocation,address_metadata,project_names,project_delivery=new_runtime
        selected=True
        known_state_dirs[candidate['dali_mac'].lower()]=state
        return dict(state='selected',connection=connection,gateway=config,state_dir=str(state),
                    config_path=str(record),stored_writes_enabled=False,lighting_commands_sent=False)

    @server.tool(annotations=read, structured_output=True)
    @expected_errors
    def dali_status() -> dict[str, Any]:
        """Local configuration/capability summary only; does not connect to devices. connectivity=not_checked does NOT prove gateway online or identity/firmware verified. No network traffic."""
        return dict(service.status(), version=__version__, gateway_selected=True) if selected else dict(version=__version__,gateway_selected=False,connectivity="not_checked",network_traffic=False)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_start_address_allocation_review(channel:StrictInt,expected_new_count:StrictInt)->dict[str,Any]:
        """Start background read-only preparation for incremental allocation on explicit channel1/2. expected_new_count1..64 is a readback assertion, NOT a device-side limit: later allocation targets ALL unaddressed gear on this channel. Backs up all existing DT6/DT8 supported parameters, groups, 16 scenes and live values; checks identity, occupancy, duplicate report and closed ConfigMaster. Keep server alive and poll get by returned operation_id until inactive. reviewed only means preparation completed; no device writes or automatic execution. Plan expires30 minutes after review. No reset, physical identity proof or atomic snapshot. Public B63 incremental allocation on the known64-lamp DT8 test board verified, followed by independent full-board audit. DT6/multiple-new-gear and fault cases remain simulated; no physical identity proof."""
        return address_allocation.start_review(channel,expected_new_count)

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_start_address_allocation(operation_id:str,confirmation:str)->dict[str,Any]:
        """Start the prepared channel-wide incremental allocation ONCE. Requires enable_experimental_address_allocation=true, exact allowed_address_allocation_digest and allowed_address_allocation_channels. Rechecks full originals, identity/occupancy, expiry and closed app, then durable claim/intent and one F001 mode2. Independent scan and full readback must preserve original devices and match expected new count. Lost receipt stays uncertain; never retry, reset, delete or automatically undo. Keep this server alive; poll get until inactive. A persisted claim prohibits replay even after restart. Does not migrate host names/layouts. Cancellation during dispatch cannot stop native allocation and still finishes necessary readback."""
        return address_allocation.start(operation_id,confirmation)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_address_allocation(operation_id:str,include_details:StrictBool=False)->dict[str,Any]:
        """Read allocation review/plan, progress, claim and execution without bus traffic or waiting for the gateway lock. Default omits large per-lamp snapshots; include_details=true returns originals/preflight/readback evidence. active_in_this_process is only this worker's liveness. A saved phase after process exit does not authorize continuation. Missing logs and worker errors remain explicit; no automatic recovery. completed_readback describes supported observed values, not physical identity or atomic state."""
        return address_allocation.get(operation_id,include_details)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_cancel_address_allocation(operation_id:str)->dict[str,Any]:
        """Request this process's allocation worker to stop before dispatch. Already dispatched native allocation cannot be cancelled/undone: finish necessary independent readback and report cancel_effect. Poll until inactive; never removes new addresses or affects another process."""
        return address_allocation.cancel(operation_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_project_delivery(project_id:str,addresses:list[StrictInt],fields:list[str],group_numbers:list[StrictInt],scenes:list[StrictInt],name_selections:list[dict[str,Any]],mark_scope:Literal['captured_marks','all_source_marks']|None=None)->dict[str,Any]:
        """Prepare ONE staged project restoration: selected DT8 parameters/scenes/group bits, then selected local names and/or floorplan. Explicit nonempty device category and local category required; empty name_selections preserves names, mark_scope=None preserves floorplan. Uses SAME immutable toolkit project for both domains. Names/floorplan share one cache replacement, both permissions required if selected. Closed ConfigMaster, file/source validation and backup before network capture, all originals before dispatch. No writes to devices/app while preparing. Not atomic across bus/file; no addressing, gateway settings, grid activation or live-output changes. B0 minimal combined field roundtrip verified; full-scope and fault coverage is simulated."""
        return project_delivery.prepare(project_id,addresses,fields,group_numbers,scenes,name_selections,mark_scope)

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_start_project_delivery(plan_id:str,confirmation:str)->dict[str,Any]:
        """Start a staged restoration once in this server. Requires enable_project_delivery and exact allowed_project_delivery_digest, PLUS each child plan's original exact digest/category/file permissions. Returns progress, not completion: keep server alive and poll get until inactive. All gates/file conflicts checked before bus writes, serial device phase then one local commit and final readback of both domains. Stops on cancellation, source drift, uncertain receipt or readback mismatch; never automatically replays or rolls back. UI rendering/physical location remain unverified. Claimed interrupted tasks must be inspected, not restarted."""
        return project_delivery.start(plan_id,confirmation)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_project_delivery(plan_id:str)->dict[str,Any]:
        """Read staged plan, claim, completed/total phases, pending category, child originals/progress and errors without network or bus-lock waiting. active_in_this_process describes only this worker. A persisted executing phase is not liveness. Missing child logs remain unavailable; no recovery/replay triggered. worker_error_in_this_process, worker-error, execution_unavailable or journal_error invalidate a clean-success interpretation, even if an older record says completed_readback. Save in-memory errors before exiting. completed_readback applies to selected device/file values at their checks, not atomic state or UI rendering."""
        return project_delivery.get(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_cancel_project_delivery(plan_id:str)->dict[str,Any]:
        """Request this process's active delivery to stop between device commands or before local commit. Already sent commands finish readback; an in-progress file replacement finishes as one commit. Poll until worker exits; cancellation does not roll back prior phases or cancel another process."""
        return project_delivery.cancel(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_project_delivery_undo(plan_id:str)->dict[str,Any]:
        """Prepare conditional undo from an original delivery's actual attempted phases and durable child evidence. Local fields first, then device values; newer unrelated file fields preserved, conflicting names/layout/device values reject. Requires worker inactive; no writes performed. Archived originals permit undo if source project was removed. Already-original is explicit; otherwise authorize NEW delivery/child digests and original category permissions before start. No auto-rollback, generated PNG deletion, or guesswork for missing receipts/journals."""
        return project_delivery.prepare_restore(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_attach_project_names(project_id:str,cache_id:str,selections:list[dict[str,Any]])->dict[str,Any]:
        """Create a NEW immutable schema4 project with selected local lamp/group/scene labels and bounded native source evidence. selections1..336 exact kind+address/group/scene, no channel/name overrides. Lamp addresses and scene numbers must be captured by project; group number is explicit. Preserves existing device capture, grids and floorplan; absent and empty labels remain distinct. No bus/app writes; local state only. Labels/source cache are not online devices, scene values or physical mapping. Existing project remains unchanged; retrieve/transfer using project tools."""
        return project_names.attach(project_id,cache_id,selections)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_project_names(project_id:str)->dict[str,Any]:
        """Read portable selected local name evidence without native source cache or bus. Missing attachment is explicit. Source includes zero-based channel/group/scene semantics; names do not change DALI scene numbers or prove online state."""
        return project_names.get(project_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_project_name_restore(project_id:str,selections:list[dict[str,Any]])->dict[str,Any]:
        """Prepare LOCAL ConfigMaster label restoration from a project's captured name attachment. Explicit selected subset only; missing/empty source labels CLEAR selected local labels, not keep-current. Closed app, matching gateway, fresh exact native backup; long/unsupported names reject without truncation. Binds source project digest and evidence; changed source before apply rejects. Apply/get/conditional undo use existing configmaster-file tools and enable_native_cache_writes gate. Undo relies on bound originals even if imported project is removed. No bus traffic/device storage, full-engineering orchestration or UI automation."""
        return project_names.prepare(project_id,selections)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_read_duplicate_addresses(channel:StrictInt)->dict[str,Any]:
        """Read one channel1/2's gateway-reported duplicate short-address bitmap (F043), with fresh gateway identity and correlated reply checks. Does not scan, allocate, delete, move or reset addresses and sends no light commands. Native application requests this after existing-address search; reply does not prove bitmap freshness or physical device uniqueness. Empty completed list means only gateway reported no duplicates. Timeout/error returns query_unknown without a fabricated empty list; never blindly resend. Raw reply and operation evidence retained locally."""
        return service.read_duplicate_addresses(channel)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_address_move(channel:StrictInt,source_address:StrictInt,destination_address:StrictInt)->dict[str,Any]:
        """Prepare one DT8 short-address0..63 move to a vacant address on explicit channel1/2. Requires ConfigMaster closed via Windows guard; fresh pinned identity, full address scan and gateway duplicate report, supported parameters/groups/all16 scenes/live backup. Occupied destination refuses without implicit deletion/swap. Ten-minute plan; no address writes. Physical uniqueness and duplicate bitmap freshness are not proven. Host names/layouts and old projects are not migrated; discard/reprepare other pending address-bound plans after moving. Never use this for reset/allocation."""
        return address_moves.prepare(channel,source_address,destination_address)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_address_removal(channel:StrictInt,address:StrictInt)->dict[str,Any]:
        """Prepare removing ONE DT8 short address0..63 on explicit channel1/2, never all-address reset. Closed ConfigMaster, fresh pinned identity, occupancy and duplicate report; save supported parameters, groups, all16 scenes and actual output. Local backup only; no device write. Ten-minute plan. Removal will make this fixture unreachable by its former short address. Backup does not prove hardware identity or enable automatic undo; incremental allocation is a separate channel-wide operation. Host labels/layouts/projects remain unchanged. Public B63 single-removal field trial and separate controlled recovery verified on the pinned test board; broader scope remains simulated."""
        return address_removal.prepare(channel,address)

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_apply_address_removal(plan_id:str,confirmation:str)->dict[str,Any]:
        """Remove the prepared single short address ONCE. Requires separate enable_experimental_address_removal=true, exact allowed_address_removal_digest and allowed_address_removals channel/address list. Rechecks occupancy and complete supported original lamp values, closed app and gates; durable claim/intent and capability invalidation before one F002 destination255. Fresh scan must show exactly the source address missing; lost receipt stays verified_receipt_unknown or stopped, never resend. Does not verify now-unaddressed lamp parameters, prove physical uniqueness, remove host labels, reset devices or automatically allocate/restore. A persisted claim prohibits replay even after restart. Call get to inspect backup/outcome."""
        return address_removal.execute(plan_id,confirmation)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_address_removal(plan_id:str)->dict[str,Any]:
        """Read removal plan, original supported lamp snapshot, claim and execution journal without gateway traffic or operation-lock waiting. Saved phase is not worker liveness. Missing log is explicit and never permits replay. No automatic recovery or writes."""
        return address_removal.get(plan_id)

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_apply_address_move(plan_id:str,confirmation:str)->dict[str,Any]:
        """Apply the prepared move once. Requires enable_experimental_address_writes=true and allowed_address_moves exact channel/source_address/destination_address entries. Fresh occupancy/duplicate/configuration checks, final vacancy check, closed-app guard, durable claim/intent and both-address capability invalidation precede one F002. Independent scan and full moved-lamp backup comparison follow. Lost receipt triggers reads only; verified_receipt_unknown retains uncertainty. Other clients are outside the shared-state-directory lock. No implicit rollback or host label/layout migration. Use get and conditional restore; this move tool does not delete or allocate."""
        return address_moves.execute(plan_id,confirmation)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_address_move(plan_id:str)->dict[str,Any]:
        """Read a saved address plan, originals, claim and execution without bus traffic or operation-lock waiting. Saved state does not prove process liveness. Missing journal after claim remains uncertain and cannot authorize replay."""
        return address_moves.get(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_address_restore(plan_id:str)->dict[str,Any]:
        """Conditionally prepare reversing a single-address move with matching durable write intent. Fresh scan must equal original or moved mapping, and the responding lamp's supported parameters/groups/all16 scenes/live must equal the saved original. Conflicting occupancy or values refuse. already_original performs no write; otherwise returns a new reverse plan requiring its exact gate and apply confirmation. Does not reconstruct deleted/unaddressed gear or migrate host names/layouts; no physical-identity proof."""
        return address_moves.prepare_restore(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_address_metadata(address_plan_id:str,components:list[Literal['lamp_name','floorplan_mark']])->dict[str,Any]:
        """Prepare moving selected local label/marker references after a verified single-address move. Fresh scan/duplicate/full moved-lamp reads must still match that plan; Windows ConfigMaster must be closed and configmaster_cache_file explicitly bound. lamp_name clears the source and transfers its label to an existing blank destination row; floorplan_mark changes only the source marker address, retaining position/image. Destination label/marker conflicts refuse. Does not migrate parameter/group caches, update image files, alter history or issue device writes. No write yet; separate metadata file gate and apply confirmation required."""
        return address_metadata.prepare(address_plan_id,components)

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_apply_address_metadata(plan_id:str,confirmation:str)->dict[str,Any]:
        """Apply selected label/marker file migration or its conditional undo once. Requires enable_native_address_metadata_writes=true, exact plan confirmation, closed app, unchanged configured cache and source move evidence; rereads bus mapping/configuration. Backs up original bytes, locks file, commits atomically and verifies bytes. No device write commands; bus and file are not one atomic transaction. Claimed plans are queried, never replayed. Parameter/group caches and background images remain unchanged; refresh native caches separately."""
        return address_metadata.apply(plan_id,confirmation)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_address_metadata(plan_id:str)->dict[str,Any]:
        """Read saved metadata migration originals, changes and file execution evidence; no bus queries. Requires intact source address-move evidence. Saved execution phase is not process liveness."""
        return address_metadata.get(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_address_metadata_restore(plan_id:str)->dict[str,Any]:
        """Prepare reversing only a label/marker migration after the physical address mapping has returned to the original addresses. Fresh source configuration must match the move backup; original file replacement intent required. Third-party target labels/marker positions/background changes refuse; unrelated later fields/markers persist. Returns already_restored or a new file plan to apply separately. Does not restore the bus itself."""
        return address_metadata.prepare_restore(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_migrate_address_layout(address_plan_id:str,layout_id:str)->dict[str,Any]:
        """Create an immutable local grid revision after a verified address move and fresh mapping/configuration reads. Selected same-channel grid must contain source and not destination; replace only that cell. Return a new layout ID, preserve original, clear physical-verification claim and record source plan/revision. Use the new ID explicitly; no active default switch, native file/parameter writes or archived project mutation. Restoring physical addresses lets you explicitly select the retained original layout; new physical verification remains separate."""
        return address_metadata.migrate_layout(address_plan_id,layout_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_configmaster_floorplan(project_id:str,mark_scope:Literal['captured_marks','all_source_marks'])->dict[str,Any]:
        """Prepare replacing the configured native cache's entire floorPlan with an embedded project floorplan. ConfigMaster must be closed; explicit configmaster_cache_file under gateways required. captured_marks includes only project capture channel/addresses; all_source_marks explicitly includes every source mark, including outside capture. Never merges with existing layout. Archive original JSON/image and source project; new exclusive PNG is created only by apply, before JSON switch. No app-file or bus changes during preparation. Returns 10-minute plan, digest and confirmation. PNG size/format limits and source provenance caveats remain. Not a complete project/device restore."""
        return native_floorplan_files.prepare(project_id,mark_scope)

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_apply_configmaster_floorplan(plan_id:str,confirmation:str)->dict[str,Any]:
        """Apply one prepared floorplan replacement or undo with enable_native_floorplan_writes=true and exact plan confirmation. Shared path lock, closed-app guards, source/current-file/image conflict checks and JSON readback. Original images never overwritten/deleted; newly created PNG retained even after undo/failure as evidence. Claimed plans never replayed; uncertain results must be inspected. File success does not prove application rendering; reopen ConfigMaster and inspect separately. No device traffic."""
        return native_floorplan_files.apply(plan_id,confirmation)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_configmaster_floorplan_plan(plan_id:str)->dict[str,Any]:
        """Read validated saved native floorplan plan and last execution journal. Does not reread live app layout or prove worker liveness/rendering. Includes source scope, backups, confirmation and retained-image policy. No application or device writes."""
        return native_floorplan_files.get(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_configmaster_floorplan_restore(plan_id:str)->dict[str,Any]:
        """Prepare conditional undo of original floorplan transaction with durable replacement intent, ConfigMaster closed. Revert only floorPlan while preserving later unrelated native cache updates. Reject newer floorplan or changed original/generated image; no blind overwrite or automatic old-image repair. Exact original JSON bytes restored only if current file exactly equals written version. New PNG retained as evidence. Apply via dali_apply_configmaster_floorplan. No device traffic."""
        return native_floorplan_files.prepare_restore(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_import_configmaster_workbook(xlsx_path:str)->dict[str,Any]:
        """Read one explicit absolute local .xlsx path (<=2MB), parse observed ConfigMaster1.2.4.1 nine-sheet format and preserve exact source bytes in toolkit evidence. Requires DALI MAC matching configured gateway; file has no IP/firmware binding and cannot prove cache versus bus provenance. Returns workbook ID, channel/address/scene counts, conflicts and percentage precision review. Local evidence only: does not import into ConfigMaster, connect to gateway, create executable restore plans, or write devices. Rejects formulas, external links and unsupported structures. Static percentage conversion is NOT runtime-verified. Use get with ID and channel for details."""
        return native_workbooks.import_file(xlsx_path)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_configmaster_workbook(workbook_id:str|None=None,channel:StrictInt|None=None)->dict[str,Any]:
        """Read saved native XLSX evidence, reparsing exact preserved bytes and checking summary/hash/configured MAC. Omit ID to list latest10 summaries; provide ID and channel1/2 for that channel's parameters, groups, names,16 ECG scenes per address, group scene blocks and conflicts. Addresses0..63/scenes0..15. Percentages remain percent; blank ordinary parameters keep, blank scene components MASK. Does not reread original file or bus, assert online/fresh state, resolve conflicts, or apply any changes."""
        return native_workbooks.get(workbook_id,channel)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_workbook_scene_restore(workbook_id:str,channel:StrictInt,addresses:list[StrictInt],scenes:list[StrictInt],scene_source:Literal['ecg','group'],group:StrictInt|None=None,brightness_policy:Literal['require_unique','native_nearest']='require_unique')->dict[str,Any]:
        """Prepare selected stored scenes from imported native XLSX: one channel, explicit unique addresses0..63 and scenes0..15 (up to64x16). Select ECG table or ONE explicit group block, never merge conflicting tables/groups. require_unique rejects ambiguous/non-native displayed percentages; native_nearest explicitly accepts the statically recovered manufacturer's nearest-display-table conversion, NOT exact original precision or verified native application behavior. Reads fresh identity, DT8/limits and ALL original scenes serially, without writing/recalling; full scope may take minutes. Returns existing10-minute scene plan/confirmation with exact source hash, targets and quantization/conflict review. Apply only authorized scope via dali_apply_scene_batch; source integrity, current limits and original scenes rechecked before writes. Use dali_get_scene_plan and dali_preview_scene_restore for result/conditional undo. Does not restore parameters, membership, names, addresses, gateway settings or live output; native file provenance remains unverified."""
        return native_scenes.prepare(workbook_id,channel,addresses,scenes,scene_source,group,brightness_policy)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_workbook_group_restore(workbook_id:str,channel:StrictInt,addresses:list[StrictInt],group_numbers:list[StrictInt])->dict[str,Any]:
        """Prepare selected group membership bits from imported native XLSX grouping sheet: explicit1..64 addresses and1..16 group numbers, one channel. Fresh identity/type/membership/live reads; preserve every unselected group bit/address. Source hash bound, never infer membership from group-scene rows or names. already_matches returns no executable plan. Otherwise apply separately authorized exact operations with dali_apply_group_membership; use its get/conditional-restore tools for evidence/undo. No group writes during preparation, no lighting/addressing/names/scene changes. File provenance remains unverified; native application import and full-project restoration are separate."""
        return native_groups.prepare(workbook_id,channel,addresses,group_numbers)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_workbook_parameter_restore(workbook_id:str,channel:StrictInt,address:StrictInt,fields:list[str],brightness_policy:Literal['require_unique','native_nearest']='require_unique')->dict[str,Any]:
        """Prepare1..9 selected native XLSX parameter fields for ONE DT8 address. Fields:fade_time,fade_rate,min_level,max_level,configured_min_ct,configured_max_ct,power_on,system_failure,dimming_curve. Curve raw0=logarithmic/1=linear. Source blanks keep current components; paired fields preserve omitted brightness/color. All blank returns no_targets_in_source without device reads. Default require_unique rejects ambiguous percentages; native_nearest explicitly accepts static manufacturer conversion, not exact originals/runtime native import proof. Fresh full supported parameters/live, source binding, existing limits and restoreability checks; no old-file CT-range expansion, fade0 or brightness255. Apply authorized plan with dali_apply_lamp_parameter and its exact full-value gate; get/conditional restore use existing tools. Preparation does not write/control devices; not multi-address bulk or full native project restore."""
        return native_parameters.prepare(workbook_id,channel,address,fields,brightness_policy)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_workbook_name_restore(workbook_id:str,channel:StrictInt,selections:list[dict[str,Any]],group_name_source:Literal['grouping','group_scene']='grouping')->dict[str,Any]:
        """Prepare selected native XLSX labels for one explicitly configured local ConfigMaster cache file, with application CLOSED. selections1..336 exact shapes:{kind:'lamp',address}, {kind:'group',group}, {kind:'scene',group,scene}; indices0-based, no name/channel overrides. Lamps take parameter-sheet names; scene notes take their group block; group names explicitly select grouping sheet(default) or group-scene block, reporting disagreements without merging. Blank means clear ONLY selected label; missing lamps, duplicate scope and unsupported names reject, never truncate. Binds source bytes/hash and selected targets, freshly backs up cache bytes and preserves all unrelated fields. Preparation does not change app file or bus. Apply/get/conditional restore use existing native-file tools with enable_native_cache_writes and exact confirmation. This is file-name restoration, NOT native application Excel import, device scene writing or complete-project restoration. Release validation defines actual UI coverage."""
        return native_workbook_names.prepare(workbook_id,channel,selections,group_name_source)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False), structured_output=True)
    @expected_errors
    def dali_preview_parameter_changes(channel:StrictInt, targets:list[ParameterReviewTarget])->dict[str,Any]:
        """Read fresh originals/live output for1..64 explicit DT8 addresses and review parameter changes, with per-lamp before/after and conditional restore candidates. Each target has address and changes:[{field,values}]. Supported fields: fade_time/fade_rate(code0..15), min_level/max_level(level_raw1..254), power_on/system_failure(level_raw0..255,kelvin_raw1000..10000 or65535; omitted component preserved), configured_min_ct/configured_max_ct(kelvin_raw1000..10000). Unknown physical CT limits prohibit expanding current configured limits. This is only a saved review, NEVER an executable parameter plan or authorization. No device writes, control, or cache changes. Inspect row rejections/partial evidence. Up to64 lamps may take minutes; saved progress is available via dali_get_parameter_review."""
        return parameters.preview(channel,[t.model_dump() for t in targets])

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_parameter_batch(channel:StrictInt,targets:list[ParameterReviewTarget])->dict[str,Any]:
        """Prepare one serial batch for1..64 explicit DT8 addresses with1..9 unique parameter fields each. Each target:{address,changes:[{field,values}]}; raw code/level/Kelvin, NOT percentages or seconds. Same nine fields as single-lamp parameters; omitted power/failure components preserved. Fresh full supported parameters and live output for EVERY target, saved originals and10-minute plan. No writes; no scene/group/address changes. Reject fade0, brightness255, un-restorable originals and CT-range expansion. Review plan entries and complete paired target values; execution is separate. May take minutes. Batch implementation has separate release-specific physical validation."""
        return parameter_batches.prepare(channel,[t.model_dump() for t in targets])

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_project_parameter_batch(project_id:str,channel:StrictInt,addresses:list[StrictInt],fields:list[str])->dict[str,Any]:
        """Prepare restoring selected supported parameter fields for1..64 captured DT8 addresses from one toolkit project, with source digest and configured/connected identity checks. Fresh originals/live for all; source cannot justify CT-limit expansion. Only selected fields; paired power/failure restores both captured components. No writes; apply through start_parameter_batch with exact multi-address gate. Not full-project restoration or live scene preview."""
        return parameter_batches.prepare_source(dict(kind='project',project_id=project_id,channel=channel,addresses=addresses,fields=fields))

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_workbook_parameter_batch(workbook_id:str,channel:StrictInt,addresses:list[StrictInt],fields:list[str],brightness_policy:Literal['require_unique','native_nearest']='require_unique')->dict[str,Any]:
        """Prepare selected native XLSX parameter fields for1..64 explicit DT8 addresses. Ordinary blanks preserve current components; addresses with all selected components blank are listed and skipped, not assumed matching. Source hash and target bindings rechecked; default require_unique rejects ambiguous percentage values, native_nearest explicitly accepts static manufacturer quantization. Reads every nonblank target's full supported originals/live; never writes. Start via separate exact batch gate. Does not execute native application import or restore names/scenes/groups; native file provenance remains unverified."""
        return parameter_batches.prepare_source(dict(kind='workbook',workbook_id=workbook_id,channel=channel,addresses=addresses,fields=fields,brightness_policy=brightness_policy))

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_start_parameter_batch(plan_id:str,confirmation:str)->dict[str,Any]:
        """Start a prepared parameter batch once in this server's background worker. Requires authorized exact scope, enable_experimental_parameter_writes=true and allowed_parameter_batch_targets:[{channel,address,values:[{field,values:FULL_TARGET}]}]. Existing single-lamp gate cannot enable a batch. All targets preflight before first write, serial shared-lock execution, per-lamp conflict recheck, durable intent, one send and full readback per field. Uncertain receipt/unexpected live output stops; no retry/automatic rollback. Keep server alive until get reports terminal AND active_in_this_process=false. This call returning is not completion. Query/cancel remain available; claimed operations must never be replayed even after restart."""
        return parameter_batches.start(plan_id,confirmation)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_parameter_batch(plan_id:str)->dict[str,Any]:
        """Get saved parameter batch plan, claim and execution steps/progress without bus access. active_in_this_process is current worker ownership, not hardware state or another process's liveness. Claimed missing/incomplete execution is uncertain, never permission to restart. completed_readback applies only to selected fields at their readback times, not atomic full-board state. Source integrity remains checked. No automatic resumption or device restoration."""
        return parameter_batches.get(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_cancel_parameter_batch(plan_id:str)->dict[str,Any]:
        """Request cancellation of a parameter batch active in THIS process. Finish current dispatched command/readback, stop before following writes, retain attempted-field evidence. Does not undo parameters or stop the server. Wait for terminal execution and active_in_this_process=false; cancellation request alone is not completion. Recovery requires prepare_parameter_batch_restore and separate exact original-value gate."""
        return parameter_batches.cancel(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_parameter_batch_restore(plan_id:str)->dict[str,Any]:
        """After a batch stops, read ALL originally selected lamps, and prepare restoring only attempted fields whose current value equals the requested target. Already-original fields skipped; third values, unrelated parameter changes or changed live output require inspection. Uses durable claim/execution prefix and original CT range evidence, never blind replay. Returns already_original or a NEW10-minute batch plan. No writes; start it only within existing restore authorization and exact original-value allowlist. Does not restore scenes/groups or unexpected live output."""
        return parameter_batches.prepare_restore(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False), structured_output=True)
    @expected_errors
    def dali_preview_project_parameter_restore(project_id:str, addresses:list[StrictInt], fields:list[str])->dict[str,Any]:
        """Review restoring1..9 explicitly selected supported parameter fields for1..64 captured DT8 addresses from a complete toolkit project. Same configured and connected gateway/IP/MAC/firmware required; fresh originals and live output read sequentially. Preserves unselected fields. power_on/system_failure select BOTH stored components; fade_time/fade_rate select only the chosen code. An old project's configured CT limits do not prove today's physical limits, so expansion is rejected. Returns a non-executable parameter review, not a write plan/confirmation. No device changes, no actual restoration. Per-row rejection or stopped_partial is not success. This review is not executable; separate single-field tools support nine bounded fields, preserving paired power/failure color and rejecting unsupported special values."""
        return parameters.preview_project(project_id,addresses,fields)

    @server.tool(annotations=read, structured_output=True)
    @expected_errors
    def dali_get_parameter_review(review_id:str|None=None)->dict[str,Any]:
        """Read saved parameter-review evidence/progress without network or waiting for the bus lock. Omit ID to list latest10. A saved reading phase does not prove a process is alive. Never resumes reads, applies candidates or restores devices; review IDs are not executable parameter plan IDs."""
        return parameters.get(review_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_fade_time(channel:StrictInt,address:StrictInt,fade_time_code:StrictInt)->dict[str,Any]:
        """Freshly read ONE explicit DT8 lamp and prepare a10-minute fade TIME change, code1..15 (raw codes, not seconds), preserving fade RATE and all other parameters. Reject code0/extended-fade originals/targets. No device writes. Future application requires explicit user authorization, separate parameter-write enablement and exact channel/address/field/value gate. Physical write/readback/restoration validation covers B0 code4->5->4, rate7 on firmware1.6.9 only; other codes/devices are not physically validated."""
        return fades.prepare_fade(channel,address,fade_time_code)

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_apply_fade_time(plan_id:str,confirmation:str)->dict[str,Any]:
        """Write an explicitly authorized fade-time-only plan once, disabled by default. Requires enable_experimental_parameter_writes=true and allowed_parameter_write_targets={channel,address,values:[{field:'fade_time',values:{code:N}}]}. Claim intent durably, reread full configuration/live state, send F018 once and read all supported parameters/output. Stops on conflict, uncertainty or unexpected output change; never automatically resends/rolls back. completed_readback plus unchanged output proves only scoped readback. Receipt status bytes remain uninterpreted. Physical validation is limited to B0 code4->5->4; broad parameter plans are rejected. No scene/group/address changes or lighting commands."""
        return fades.execute(plan_id,confirmation)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_fade_time_plan(plan_id:str|None=None)->dict[str,Any]:
        """Read local fade-time plan, claim and execution without bus traffic or waiting for the transaction lock; omit ID to list latest10 candidate records. Excludes other parameter fields. Claimed but missing execution is uncertain and must not be rerun. Saved phase does not prove process liveness."""
        return fades.get(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_fade_time_restore(plan_id:str)->dict[str,Any]:
        """Freshly read current values and review restoring an attempted fade-time plan's original time code. Preserves rate and all other fields; refuses unrelated configuration changes or values matching neither original nor attempted target. No restoration yet; applying requires explicit restoration authorization and exact original-code scope. already_original means no write needed. No automatic replay after failure or process exit."""
        return fades.prepare_restore(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_lamp_parameter(channel:StrictInt,address:StrictInt,field:Literal['fade_time','fade_rate','min_level','max_level','configured_min_ct','configured_max_ct','power_on','system_failure','dimming_curve'],value:StrictInt)->dict[str,Any]:
        """Freshly read ONE DT8 lamp and prepare ONE10-minute parameter change. value: fade_time/fade_rate raw code1..15, min_level/max_level raw1..254, configured_min_ct/configured_max_ct Kelvin1000..10000, power_on/system_failure raw brightness0..254, dimming_curve raw0=logarithmic/1=linear (B0 raw0->1->0 storage roundtrip verified; photometric response unmeasured). Paired power/failure color is preserved, including65535; this scalar tool cannot change it. Original must be restorable. CT limits may narrow the current configured range; expansion requires a restoration plan with original-range evidence, not guessed physical limits. No device writes. Apply needs exact channel/address/field/full-value scope. Readback may quantize Kelvin. Scoped B0 field trials passed on firmware1.6.9 with output OFF; other devices/values and actual power-cycle/fault behavior are not physically validated. No multi-field plan."""
        return lamp_parameters.prepare_parameter(channel,address,field,value)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False), structured_output=True)
    @expected_errors
    def dali_prepare_project_parameter_restore(project_id:str,address:StrictInt,fields:list[str])->dict[str,Any]:
        """Prepare an executable restoration of1..9 selected parameter fields for ONE captured DT8 lamp from a complete toolkit project. Fresh full parameter/live backup and matching configured/connected gateway required. Selected power_on/system_failure restore both components; unselected fields stay current. No device writes in preparation. Apply with dali_apply_lamp_parameter using its exact scope gate; inspect each readback and stop state. Get/undo use dali_get_lamp_parameter_plan/dali_prepare_lamp_parameter_restore. CT expansion still requires independent physical-limit evidence and is rejected; old project limits are not proof. Raw fade0/brightness255 remain unsupported. This does not restore scenes, groups, live output, addresses, or a full native ConfigMaster project."""
        return lamp_parameters.prepare_project(project_id,address,fields)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_power_parameter(channel:StrictInt,address:StrictInt,field:Literal['power_on','system_failure'],level_raw:StrictInt|None=None,kelvin_raw:StrictInt|None=None)->dict[str,Any]:
        """Prepare one DT8 lamp's power-on or system-failure stored brightness/color pair, not live output. Supply at least one component: level_raw0..254 (not percent), kelvin_raw1000..10000 within configured limits or65535 raw MASK. Omitted/null components preserve freshly read originals. Both may change in one field; raw brightness255 is not yet supported, including as an original. Original finite color must be restorable within configured limits. Reads all supported parameters and current output, saves a10-minute plan, writes no device. Apply/get/restore use the lamp-parameter tools with the complete resulting pair in the exact write gate. Kelvin may quantize. See release validation for field/value/device evidence; this does not establish actual power-cycle/fault behavior."""
        return lamp_parameters.prepare_power_parameter(channel,address,field,level_raw=level_raw,kelvin_raw=kelvin_raw)

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_apply_lamp_parameter(plan_id:str,confirmation:str)->dict[str,Any]:
        """Apply one authorized supported parameter plan once. Disabled by default: enable_experimental_parameter_writes=true plus allowed_parameter_write_targets={channel,address,values:[{field,values:FULL_TARGET_VALUE}]} must match proposal.steps[].after; paired power/failure entries include both resulting level_raw and kelvin_raw. Durable claim, fresh conflict checks, one send, terminal receipt wait for CT/power fields, full parameter/live readback and capability invalidation. Unexpected live changes stop. No automatic resend, rollback or light control. Lost receipt is read-only reconciled and stays uncertain even if target matches. Accept multi-field plans only when bound to a validated toolkit project; reject unbound generic multi-field plans and unsupported values. completed_readback proves this scoped storage readback, not timing, power-cycle or fault behavior."""
        return lamp_parameters.execute(plan_id,confirmation)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_lamp_parameter_plan(plan_id:str|None=None)->dict[str,Any]:
        """Read saved supported single-field or project-bound multi-field parameter plan/claim/execution without bus traffic or waiting for its lock. Excludes unsupported or unbound multi-field records. Omit ID for latest10 candidates. A saved stage does not prove liveness. Claimed but missing execution is uncertain and must not be replayed."""
        return lamp_parameters.get(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_lamp_parameter_restore(plan_id:str)->dict[str,Any]:
        """Freshly read current state and prepare restoring only attempted supported fields to their original values, including project-bound multi-field plans. Reject third values, unrelated parameter changes and unsupported or unbound multi-field ancestry. already_original needs no write. Applying restoration requires explicit restoration scope and exact original-value enablement; never blind undo/replay. Does not restore live brightness/color if the previous limit change affected output, and sends no live commands."""
        return lamp_parameters.prepare_restore(plan_id)

    @server.tool(annotations=read, structured_output=True)
    @expected_errors
    def dali_discover() -> dict[str, Any]:
        """Discover the configured gateway by unicast and report identity/firmware. No lamp commands."""
        return service.discover()

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_scan_existing(channel:int)->dict[str,Any]:
        """Search existing short addresses on one channel (1=A,2=B), equivalent to the UI's search-existing-addresses button. Fixed read-only mode: never allocates, readdresses, changes groups/scenes, or lights lamps. May take up to four minutes. Returns the gateway's fresh bitmap and addresses 0..63. Scan timeout/error is scan_unknown, NOT an empty channel or proof that all lamps are offline. Do not automatically resend. Does not report duplicate addresses, detailed lamp faults, or lamp type."""
        return service.scan_existing(channel)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_read_groups(channel:int,addresses:list[int])->dict[str,Any]:
        """Read groups 0..15 for 1..64 explicitly requested short addresses; no group edits or lighting changes. This is membership per requested lamp, not necessarily a complete list of all members of any group. Stops on a failed read, preserves partial rows and pending_address; never treats an unqueried or failed address as a nonmember."""
        return service.read_groups(channel,addresses)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False), structured_output=True)
    @expected_errors
    def dali_recall_scene(channel: int, scene: int, verify_addresses: list[int]) -> dict[str, Any]:
        """Change live lighting: recall an existing scene on the ENTIRE explicitly specified channel (1=A,2=B), once. Requires user request to activate this scene/channel. Does not rewrite stored scenes; independent of experimental storage-write switch. Verify 1..8 sample addresses using live brightness/CT reads. Gateway receipt alone does not prove physical effect. Never automatically resend on timeout."""
        return service.recall_scene(channel,scene,verify_addresses)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_recall_lamp_scene(channel:StrictInt,address:StrictInt,scene:StrictInt)->dict[str,Any]:
        """Change live output: recall existing scene0..15 at exactly one short address0..63 on channel1=A/2=B, once. Never expands to a group/channel or rewrites scene storage. Fresh DT8 scene, original output and bounded fade are recorded before dispatch; MASK preserves the prior component and all-MASK cannot prove recall effect. Receipt uncertainty triggers independent readback, never resend. Check receipt_status and verification_status separately; log errors remain outcome_unknown even when output matches. Other lamps and physical address uniqueness are not verified by this operation. No automatic restoration. Hardware acceptance covers B0 scene8 on the tested firmware1.6.9 DT8 board; other addresses/channels and failure paths have model coverage."""
        return service.recall_lamp_scene(channel,address,scene)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_lamp_recall(operation_id:str|None=None)->dict[str,Any]:
        """Read a saved single-address recall record, or latest10, without device traffic. Retains original output, target scene, receipt, readback and any errors. Saved intent is not proof of dispatch or a live process. Does not resume, resend or restore."""
        return service.get_lamp_recall(operation_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_recall_group_scene(channel:StrictInt,group:StrictInt,scene:StrictInt,expected_addresses:list[StrictInt],verify_addresses:list[StrictInt])->dict[str,Any]:
        """Recall one existing scene0..15 on one entire group0..15 of channel1=A/2=B, once, changing live lighting only. Supply the expected complete member list and1..8 member samples. Fresh address scan and all discovered memberships must match exactly before sending. Saves each member's original output and scene values, checks DT8/known bounded fades, then sends one group recall. No group edits, scene writes, broadcast expansion or automatic rollback. Scan can take4minutes; keep the process alive. Unknown receipt stops without resend, then a fresh session reads samples. MASK components must preserve their previous values; all-MASK samples cannot prove recall effect. Inspect receipt_status, verification_status and samples_verified separately. Samples do not verify all members/nonmembers. No cross-program lock; saved stages are not proof a process is alive. Physical validation status is documented in the release."""
        return service.recall_group_scene(channel,group,scene,expected_addresses,verify_addresses)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_group_recall(operation_id:str|None=None)->dict[str,Any]:
        """Read saved group-recall originals, pending phase, receipt and sample verification without gateway traffic or waiting for the operation lock. Omit ID to list latest10. Saved in-progress state is not proof a process is running; never resumes, resends, or restores anything."""
        return service.get_group_recall(operation_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_import_configmaster_cache(document:dict[str,Any])->dict[str,Any]:
        """Import an explicit ConfigMaster1.2.4.1 per-gateway JSON cache as LOCAL evidence. Requires matching configured IP/DALI MAC/firmware, without connecting. Extracts channel-scoped lamp/group names and group-scoped scene names; preserves complete original JSON. Cached addresses are NOT online scan results, cached parameters/scenes are NOT live evidence, zero placeholders are NOT proven OFF. Does not restore devices, update capabilities, modify ConfigMaster files, or import Excel/full multi-gateway exports."""
        return service.import_configmaster_cache(document)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_configmaster_cache(cache_id:str|None=None,include_document:StrictBool=False)->dict[str,Any]:
        """Read a verified local cache record or list latest10. Explicit ID plus include_document=true exports the preserved native JSON as data, without writing the source application. No network calls. Checks integrity and configured gateway match; labels are local cache evidence, never device scene parameters or online status."""
        return service.get_configmaster_cache(cache_id,include_document)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_inspect_configmaster_floorplan(cache_id:str)->dict[str,Any]:
        """Inspect an imported native cache's floorPlan: PNG leaf reference and channel/address markers in image-width/height fractions. Local evidence only; absent floorPlan differs from an empty mark list. Does not open the referenced image, access the gateway, create a grid, or verify physical positions. Rejects malformed/duplicate markers and unsafe filename references; preserves finite out-of-image coordinates with a flag."""
        return service.inspect_configmaster_floorplan(cache_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_attach_project_floorplan(project_id:str,cache_id:str,image_path:str)->dict[str,Any]:
        """Create an immutable project revision with native floor-plan source JSON, coordinates and embedded PNG bytes. Explicit absolute local image_path is required: native filename is not resolved or fetched, and the caller must select the intended background. Supports <=2MB noninterlaced PNG, <=4M pixels. Gateway identity must match; selects only markers inside the project's captured channel/address scope while preserving other markers as source evidence. No application-file/device writes, no live traffic, no grid conversion or physical verification. Preserves existing project grids; total project remains <=5MB. Retrieve/transfer with existing project tools; does not restore native files."""
        return service.attach_project_floorplan(project_id,cache_id,image_path)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_project_floorplan(project_id:str,include_image:StrictBool=False)->dict[str,Any]:
        """Read validated embedded native floor-plan evidence, selected markers and image metadata from a project, without source files or bus traffic. include_image=true returns the exact PNG as base64 for an explicit export; default omits image bytes. No physical verification, device control or native-file restore."""
        return service.get_project_floorplan(project_id,include_image)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_configmaster_names(cache_id:str,changes:list[dict[str,Any]])->dict[str,Any]:
        """Prepare LOCAL native JSON name edits: each change has kind lamp/group/scene, channel1/2, name, and address0..63 for lamp, group0..15 for group, group+scene0..15 for scene. At most30 UTF8 bytes per name, no control characters; empty string clears label. Rejects duplicate targets or missing lamp rows. Preserves all other JSON data. Saves a reviewable plan only; never writes application files/devices."""
        return service.prepare_configmaster_names(cache_id,changes)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_configmaster_name_plan(plan_id:str,include_document:StrictBool=False)->dict[str,Any]:
        """Read a validated native name plan and optionally its draft JSON. Rebuilds draft from original imported cache and strict edits. This is a draft, not an applied rename; use fresh-current comparison before considering application-file replacement."""
        return service.get_configmaster_name_plan(plan_id,include_document)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_rebase_configmaster_names(plan_id:str,current_document:dict[str,Any])->dict[str,Any]:
        """Compare a name plan against caller-supplied current native JSON. Preserve unrelated changes; refuse a whole draft if a target label changed to a third value or a lamp row disappeared. Desired labels already present are no-ops. Returns merged draft only when conflict-free. Does not read the actual source file, guarantee freshness, write application files, or control devices."""
        return service.rebase_configmaster_names(plan_id,current_document)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_read_configmaster_file()->dict[str,Any]:
        """Read the explicitly configured configmaster_cache_file, validate schema/gateway identity and import its names as local evidence. Returns byte SHA256 and cache ID; does not control devices or write the source file. The app may be open for this read; disk content is not app-memory or bus state. No arbitrary file path argument."""
        return native_files.read()

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_configmaster_file_names(changes:list[dict[str,Any]])->dict[str,Any]:
        """Prepare a10-minute actual-file name plan and durable exact-byte backup. ConfigMaster must be closed. Edits use existing lamp/group/scene name schema (channel1/2, indices0-based, <=30 UTF8 bytes). Reads the one configmaster_cache_file, checks gateway identity, preserves unrelated JSON values. Writes only toolkit evidence, not app file or devices. Returns exact diff and confirmation for separately authorized application."""
        return native_files.prepare(changes)

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_apply_configmaster_file_names(plan_id:str,confirmation:str)->dict[str,Any]:
        """Apply a prepared name-file or scoped restore plan once, requiring enable_native_cache_writes=true and matching confirmation. Windows only; ConfigMaster must remain closed and file unchanged since preparation. Uses native replacement preserving ACLs, then byte readback. Sidecar OS lock covers toolkit writers on this path, not app/editor launch. No network or device commands. Do not interpret file success as app reload success. On error inspect get/actual file, never replay a claimed plan; prepare a conflict-checked restore separately."""
        return native_files.apply(plan_id,confirmation)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_configmaster_file_plan(plan_id:str)->dict[str,Any]:
        """Read validated file plan, backup hashes, confirmation and execution journal. No bus or application-file write. Claim without terminal journal is uncertain, not safe to repeat. Requires unchanged configured file binding; checks plan and backup integrity. Does not infer process liveness from saved phase."""
        return native_files.get(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_configmaster_file_restore(plan_id:str)->dict[str,Any]:
        """Prepare restoration of only this transaction's original names while ConfigMaster is closed. Requires original durable write intent; changed-to-third-value target names block the whole restore, unrelated fresh fields are preserved. Missing/empty labels remain distinct; containers created by this transaction are removed only if still empty. If the file has no intervening changes, restore exact original bytes. Already-original names return already_restored. Apply returned plan using explicit file-write gate and confirmation; no automatic restoration or device traffic."""
        return native_files.prepare_restore(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_group_membership(channel:StrictInt,changes:list[dict[str,Any]])->dict[str,Any]:
        """Freshly read explicit short addresses and create a10-minute group-membership plan. Each edit is {address:0..63,add:[groups0..15],remove:[groups0..15]}; require unique addresses and disjoint lists. Keeps all other membership bits, records original live output; no writes. Remove-before-add ordering may cause temporary membership gaps. Do not prepare broad unrequested scopes."""
        return groups.prepare(channel,changes)

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_apply_group_membership(plan_id:str,confirmation:str)->dict[str,Any]:
        """Execute an authorized group plan once. Requires separate enable_experimental_group_writes=true and allowed_group_write_operations exact channel/address/group/member entries. Fresh original/member/live checks, durable intent before each F015, full membership readback, final target-scope verification. Unknown receipt stops, reconnects for reads only, never resends. No automatic rollback. Get journal and prepare an explicitly authorized restore if needed. OS lock only covers group controllers sharing a state dir, not other tools or ConfigMaster. Physical validation is limited to B0 group15 add/remove on DT8 firmware1.6.9."""
        return groups.execute(plan_id,confirmation)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_group_membership_plan(plan_id:str|None=None)->dict[str,Any]:
        """Get group plan originals, confirmation and saved execution/claim, or list latest10. No bus traffic or waiting for the group-operation lock. Saved phase does not prove process liveness; missing run after claim remains uncertain and cannot be retried automatically."""
        return groups.get(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_group_restore(plan_id:str)->dict[str,Any]:
        """Read current membership/live state and prepare a separate plan to restore a consumed group plan's originals. Accepts only states matching the durable attempted prefix; rejects unrelated membership/live changes. Does not restore immediately; application needs explicit restoration authorization, separate exact-scope gate and returned confirmation."""
        return groups.prepare_restore(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_preview_project_group_restore(project_id:str,addresses:list[StrictInt],group_numbers:list[StrictInt])->dict[str,Any]:
        """Preview restoring explicitly selected group0..15 membership bits for selected captured short addresses0..63 from a complete toolkit project. Freshly reads full memberships, fixture types and live output; requires same configured gateway and fixture types. Preserves all unselected current group bits/addresses; no lighting, scene, parameter or membership writes. Returns already_matches without a plan if all selected bits match; otherwise returns a10-minute group plan with source digest and fresh originals. Apply only with user authorization via dali_apply_group_membership and separate exact-operation gate; undo that application with dali_prepare_group_restore. Imported checksums do not authenticate provenance. Does not accept ConfigMaster cache or prove all members of a group were captured. Preparation and broader scopes are not physical write validation."""
        return groups.prepare_project_restore(project_id,addresses,group_numbers)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_read_actual(channel: int, addresses: list[int]) -> dict[str,Any]:
        """Read CURRENT live brightness/CT for explicit addresses, distinct from stored scene values. Does not recall or modify scenes."""
        return service.read_actual(channel,addresses)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_blink_lamp(channel:StrictInt,address:StrictInt,cycles:StrictInt=3,period_ms:StrictInt=2000)->dict[str,Any]:
        """Finite native brightness blinking of ONE explicitly authorized DT8 short address, then original-brightness restoration. cycles1..100; period_ms400..60000 in100ms steps; cycles*period_ms<=60000. Full period includes on+off;400/2000ms are factory UI fast/slow defaults, not current UI settings. Sends native recall-maximum/off, never changes color/scenes/parameters. Requires stable valid original and known restoration fade<=5s. At periods>=2000ms reads each phase; faster cycles record receipts/timestamps without claiming physical phase verification. Scheduling overrun stops, never bursts to catch up. completed_verified_restored requires all phase samples and original output; dispatched_restored verifies final restoration only. Failure/cancellation stops phases and attempts original brightness once with readback. Inspect restore_verified; forced process exit cannot guarantee restoration. Keep stdio alive. Use dali_get_blink then dali_cancel_blink for an active operation, never automatically replay. Physical scope: B0 two2000ms cycles with all4 phase readbacks; three400ms cycles receipts/timestamps and final restoration only; cancellation restores originally-lit raw200. UI clicking, other fixtures/periods and physical fast-cycle timing unverified."""
        return blinks.execute(channel,address,cycles,period_ms)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_blink(operation_id:str|None=None)->dict[str,Any]:
        """Read saved blink originals, phase receipts/timestamps/samples and restoration evidence without network or waiting for bus lock. Omit ID for latest10. active_in_this_process is this server's active map, not proof an old operation in another process is alive. Never automatically resumes or restores."""
        return blinks.get(operation_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_cancel_blink(operation_id:str)->dict[str,Any]:
        """Request cancellation of an active finite blink in this server. Interrupts holds and prevents future phases; an in-flight network call may finish first. The worker then reads/restores original brightness. cancel_requested is NOT completion or restoration; poll dali_get_blink and keep process alive. Does not replay old records or independently send a command."""
        return blinks.cancel(operation_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_set_lamp_level_extreme(channel:StrictInt,address:StrictInt,extreme:Literal['minimum','maximum'])->dict[str,Any]:
        """Send ONE native recall-minimum/maximum command to an explicitly authorized DT8 short address. Fresh configured min/max and stable original output are read first; minimum is NOT OFF and maximum may be below254. No scene, group, address, color or parameter-storage commands. Verify native output against the freshly read limit and unchanged color. Inspect state, receipt_status, target_verified and actual; recorded intent or receipt alone is not output proof. Lost receipt uses a fresh identity-checked read connection without retransmission and remains stopped_receipt_unknown. No automatic restore; saved original is available via dali_get_level_extreme. Physical acceptance: firmware1.6.9 DT8 B0 configured maximum254/minimum2 at4000K; all64 background and final restoration independently read back. Other fixtures/configured limits remain model-tested."""
        return service.set_level_extreme(channel,address,extreme)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_level_extreme(operation_id:str|None=None)->dict[str,Any]:
        """Read local native minimum/maximum operation evidence; omit ID for latest10. No device traffic, replay or restoration. Saved intent does not prove dispatch or process liveness."""
        return service.get_level_extreme(operation_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_relative_dim(channel:StrictInt,address:StrictInt,direction:Literal['up','down'])->dict[str,Any]:
        """Send ONE native relative brightness command to one explicitly authorized DT8 short address. This is up/down, NOT a requested percentage increment or an absolute target; step size depends on the device. Fresh stable original output required. No scene, color or parameter-storage commands. Read output independently after the correlated receipt; inspect state, original, actual, direction_matches and color_unchanged. No_change_observed is not proof of a successful change. Unknown receipt remains outcome_uncertain even if output moved. Never automatically repeat or restore; a repeat would apply a second change. Use dali_get_relative_dim after losing the client response. Physical acceptance covers B0 raw200->209->200 at4000K on pinned firmware1.6.9; endpoints, other rate codes and other fixtures are not physically validated."""
        return service.relative_dim(channel,address,direction)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_relative_dim(operation_id:str|None=None)->dict[str,Any]:
        """Read saved native relative-dimming originals, receipt and readback without network or bus lock. Omit ID for latest10. Never replays or restores; saved dispatch_intent does not prove whether the command was sent or the process is alive."""
        return service.get_relative_dim(operation_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_identify_lamp(channel:StrictInt,address:StrictInt,hold_seconds:StrictInt=2)->dict[str,Any]:
        """Physically identify ONE explicitly authorized DT8 lamp: read stable original output, briefly switch off if lit, then to its configured maximum, then restore original brightness. Hold each reached level 1..5 seconds (default2). No color, scene, group, addressing or fade-setting writes. Refuses unknown/long fades, unknown original color or changing output before control. Every transition is read back; receipt uncertainty stops further identification and attempts original-brightness restoration with fresh reads. This restoration is part of the requested operation. Inspect restore_verified/state; failure may leave changed output. Never retry automatically. Keep process alive: forced process exit cannot guarantee restoration. Saved original and phase are available with dali_get_identification; saved phase alone does not prove a job is running."""
        return service.identify_lamp(channel,address,hold_seconds)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_identification(operation_id:str|None=None)->dict[str,Any]:
        """Read identification evidence without bus traffic or waiting for the bus lock. Omit ID to list the latest10 records, useful after a lost client response. Includes originals, attempted transitions and restoration verification. A saved in-progress phase is not proof the owning process is alive. Does not resume, resend or restore anything."""
        return service.get_identification(operation_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    async def dali_set_live_output(channel:int,changes:list[LiveChange],verify_addresses:list[int],ctx:Context)->dict[str,Any]:
        """Immediately change live brightness/Kelvin for 1..64 explicit DT8 short addresses on one channel. Use FIRST when designing/tuning scenes; let the user see the effect BEFORE saving scene storage. Omit a component to preserve it. Raw levels are not percentages. Reuses pinned-gateway capability profiles up to 24h old; use dali_read_lamp_profiles to refresh after fixture/settings changes. No stored scene writes or scene recalls. Requires user authorization to adjust these lamps. Checks all parameters before dispatch; samples 1..8 targeted lamps afterwards. On uncertain command outcome STOP, read actual state, and never blindly retry. Returned receipt is not physical proof; inspect samples_verified and state."""
        return await run_with_progress(ctx,service.set_live_output,channel,[c.model_dump(exclude_none=True) for c in changes],verify_addresses)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    async def dali_set_channel_live_output(channel:int,verify_addresses:list[int],ctx:Context,level_raw:int|None=None,kelvin_raw:int|None=None)->dict[str,Any]:
        """FAST realtime preview by explicit ENTIRE-CHANNEL broadcast. Use ONLY when user authorizes all 64 addresses on this channel with identical parameters, never for a subset/group. No scene storage writes/recall. Reuses verified type/CT/fade capabilities for 24h; first use queries missing capabilities. Waits the longest verified fixture fade before brightness can interrupt CT; then checks 1..8 samples. Omit a component to preserve it. Unknown/long fades reject before dispatch; use dali_set_live_output instead. Inspect samples_verified and state. Do not blindly resend on timeout."""
        return await run_with_progress(ctx,service.set_channel_live_output,channel,level_raw,kelvin_raw,verify_addresses)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    async def dali_set_group_live_output(channel:StrictInt,group:StrictInt,expected_addresses:list[StrictInt],verify_addresses:list[StrictInt],ctx:Context,level_raw:StrictInt|None=None,kelvin_raw:StrictInt|None=None)->dict[str,Any]:
        """Preview identical live settings on ONE explicitly authorized entire group0..15 of channel1=A/2=B, using a group command, never a channel broadcast. Supply its expected complete address list, not just verification samples. Fresh existing-address scan and memberships must agree before control; scope_changed/preflight_failed sends no controls. Scan may take four minutes. Membership reads are sequential, not atomic; no cross-program lock. Confirmed DT8 capabilities and all-member fade bounds guard CT then brightness. Omit a component to preserve it. No group edits, scene writes or recalls. Verify1..8 members; samples are not whole-group proof. On uncertainty stop, inspect live state, never automatically resend."""
        return await run_with_progress(ctx,service.set_group_live_output,channel,group,expected_addresses,verify_addresses,level_raw,kelvin_raw)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_read_lamp_profiles(channel:int,addresses:list[int])->dict[str,Any]:
        """Refresh lamp type, configured Kelvin limits, dimming curve and fade timing caches before authoring scenes. No device writes."""
        return service.read_profiles(channel,addresses)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_diagnose_lamps(channel:StrictInt,addresses:list[StrictInt])->dict[str,Any]:
        """Read fresh supported parameters plus actual output for1..16 explicit addresses0..63, channel1=A/2=B. Serial only, no automatic repair, scan or retry. Report type/range/unknown-value inconsistencies; status bits stay raw and no_inconsistency_observed is NOT proof of physical health. OFF is not a below-minimum defect. Read or identity failure is not proof of offline; stops with exact pending field, preserved partial values and uncompleted addresses. Does not refresh capability cache or read scenes, physical CT limits, extended fade duration or device version. Field checked on two DT8 addresses with firmware1.6.9; no fault-bit decoding or physical health claim."""
        return service.diagnose_lamps(channel,addresses)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_diagnostic(operation_id:str|None=None)->dict[str,Any]:
        """Read a saved diagnostic report or the latest10, with no device traffic. Not a fresh online check or proof of a running process. Never resumes, retries or repairs."""
        return service.get_diagnostic(operation_id)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_read_lamp_parameters(channel:StrictInt,addresses:list[StrictInt])->dict[str,Any]:
        """Read single-lamp configuration fields fresh for 1..16 explicit addresses, serially, without device writes. Includes type, raw status, fade time/rate codes, min/max level, power-on and system-failure level/DT8 Kelvin, dimming curve, configured CT limits, and groups. Raw level is NOT percent; 255/65535 must remain sentinel values. Does not test power-loss/failure behavior, read scenes/live output, physical CT limits, extended fade or device version. Not an atomic snapshot. On failure stops with pending address+field and partial values; omitted fields are unknown, not zero. DT6 CT limits are not_applicable, not zero. Does not refresh the separate capability cache; use dali_read_lamp_profiles for that purpose."""
        return service.read_lamp_parameters(channel,addresses)

    @server.tool(annotations=read, structured_output=True)
    @expected_errors
    def dali_read_scenes(channel: int, addresses: list[int], scenes: list[int]) -> dict[str, Any]:
        """Read explicit lamp scenes (maximum 64 pairs), auto-query lamp type, save snapshot. Channel 1..2; addresses 0..63; scenes 0..15. Takes a temporary gateway session."""
        return service.read_scenes(channel, addresses, scenes)

    @server.tool(annotations=read, structured_output=True)
    @expected_errors
    def dali_preview_scene_batch(changes: list[SceneChange]) -> dict[str, Any]:
        """Read current values and preview up to 1024 explicit DT8 scene changes across scenes, storing originals. No device writes. Raw DALI levels are NOT percentages. Check per-fixture Kelvin limits before applying. Plans expire after 10 minutes."""
        return service.preview([c.model_dump() for c in changes])

    @server.tool(annotations=read, structured_output=True)
    @expected_errors
    def dali_get_scene_plan(plan_id: str) -> dict[str, Any]:
        """Read a stored preview or execution report, including original values and any uncertain/partial results."""
        return service.get_plan(plan_id)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_preview_scene_restore(plan_id:str)->dict[str,Any]:
        """Read current stored values and review restoration of only attempted writes in a completed/stopped_partial/cancelled_partial scene plan. Unchanged and unattempted targets are excluded. Current values must match either original or attempted values; third values produce restore_conflict and no executable confirmation. No device writes, no automatic rollback/resume; any apply requires explicit restore scope authorization and current scoped write gate. Saved local evidence is not an authenticity guarantee."""
        return service.preview_scene_restore(plan_id)

    @server.tool(annotations=write, structured_output=True)
    @expected_errors
    def dali_apply_scene_batch(plan_id: str, confirmation: str) -> dict[str, Any]:
        """EXPERIMENTAL: write authorized scene storage changes, then verify each by readback. Disabled by default. Requires the exact confirmation string from preview AND explicit user authorization of those changes. results[].status: verified=written and readback matched; unchanged=fresh value already matched, success without write; outcome_unknown=not verified; verification_failed=readback mismatch. Require state=completed AND all requested results verified/unchanged for full success. Stop on failure; never retry or rollback blindly. Does not recall scenes. Never enable simply to bypass a disabled-write error. Prior read-only metadata is preserved under preview_metadata. Execution device_writes and write_intent_count describe recorded intent, not proof of dispatch, receipt or successful readback; all unchanged means false/0."""
        return service.apply(plan_id, confirmation)

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_save_scene_batch(changes:list[SceneChange])->dict[str,Any]:
        """FAST save of already authorized/visually accepted scene values, 1..1024 explicit address/scene targets. Requires user request to SAVE these values, not just preview lighting. Disabled by default; same scoped storage-write gate. In one serial session read fresh originals for ALL targets, persist backup, then write each changed target and verify its stored value. Reuse capability profiles; skip truly unchanged values including Kelvin quantization. No duplicate preflight round-trip, no scene recall. Full per-lamp verification retained. Stops partial on uncertainty; never retry or roll back blindly. For changes requiring a separate review, use preview + apply instead."""
        return service.save_scene_batch([c.model_dump() for c in changes])

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_start_scene_save(request_id:str,changes:list[SceneChange])->dict[str,Any]:
        """Start an explicitly authorized save of up to1024 address/scene targets; per-scene progress is reported; return job_id immediately. Same write gate/scope/backup/readback as dali_save_scene_batch. Reuse the SAME request_id for transport retries: it returns the prior job and never resends it. Different content with that ID is rejected. Keep this MCP process/stdio connection alive until terminal; closing it interrupts work. Poll dali_get_job; do not start a new ID after uncertainty without reading the old job and device state."""
        return jobs.start_save(request_id,[c.model_dump() for c in changes])

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_job(job_id:str,include_result:bool=False)->dict[str,Any]:
        """Read persisted save job progress without waiting for the bus lock. include_result returns the full backup/write/readback result when available. After server restart, unfinished jobs are interrupted, never automatically resumed. plan_id links partial evidence."""
        return jobs.get(job_id,include_result)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_cancel_job(job_id:str)->dict[str,Any]:
        """Request cancellation between lamp operations. A sent write is still read back; no automatic rollback. cancel_requested is acknowledgement, not completion: poll until terminal. Does not resend or undo any commands."""
        return jobs.cancel(job_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_create_layout(name:str,channel:StrictInt,address_grid:list[list[StrictInt|None]])->dict[str,Any]:
        """Store an explicit physical layout locally, bound to this configured gateway. Grid rows run top-to-bottom, columns left-to-right; unique addresses0..63, null means empty. No bus traffic or automatic mapping verification. Never infer a new project's layout from another test board."""
        return service.create_layout(name,channel,address_grid)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_layout(layout_id:str|None=None)->dict[str,Any]:
        """Read one saved layout or list latest50. No bus traffic. Reject corrupt or differently bound layouts; no automatic remapping."""
        return service.get_layout(layout_id)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_export_layout(layout_id:str)->dict[str,Any]:
        """Export an integrity-checked toolkit layout as JSON, including gateway identity and any human check record. No bus traffic or device changes. Not a ConfigMaster floorplan."""
        return service.export_layout(layout_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_import_layout(document:dict[str,Any])->dict[str,Any]:
        """Import a dali_export_layout document into a new local ID, bound to the same configured gateway/IP/firmware. Never overwrites mappings or remaps addresses. Resets physical verification; imported human claims do not prove the current installation. No bus traffic."""
        return service.import_layout(document)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_record_layout_check(layout_id:str,expected_digest:str,checked_addresses:list[StrictInt],note:str,checked_by:str)->dict[str,Any]:
        """Record an actually performed HUMAN physical mapping check as a new immutable layout revision. Supply current digest, exact checked addresses, description and checker name; never invent an observation. Full mapped-address coverage sets physical_mapping_verified, explicitly sourced as human_attestation, not automated proof. Partial checks remain unverified. Does not turn lamps on or query devices."""
        return service.record_layout_check(layout_id,expected_digest,checked_addresses,note,checked_by)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_live_operation(kind:Literal['live-output','live-channel','live-group'],operation_id:str|None=None)->dict[str,Any]:
        """Read live adjustment evidence/progress without the bus lock or network. live-output=explicit addresses; live-channel=broadcast; live-group=group. Omit ID for latest10, useful while synchronous control is still running in the SAME live server. Profiles/fade preflight includes pending address, cache/bus source and stage count. A saved phase is not process liveness; sample verification does not cover all fixtures. Never reissue a control just because its response is slow."""
        return service.get_live_operation(kind,operation_id)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_prepare_live_output(channel:StrictInt,changes:list[PercentChange],scene:StrictInt|None=None)->dict[str,Any]:
        """Prepare explicit DT8 targets without changing lighting or saving scenes. Convert percentages using confirmed standard logarithmic curve0 and min/max brightness; reject unsupported curves or out-of-range targets, never silently clamp. Read missing capabilities, reuse identity-bound caches up to24h. Returns ready live_arguments and optional scene_changes for literal scene0..15. Omitted scene component becomes MASK, not a captured live value. Preparation is not preview: execute authorized live_arguments, inspect effect, then save only when requested."""
        return service.prepare_live_output(channel,[c.model_dump(exclude_none=True) for c in changes],scene)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_compose_layout_output(layout_id:str,foreground_percent:StrictFloat|StrictInt,background:Literal['off','preserve'],kelvin_raw:StrictInt|None=None,glyph:Literal['X','M','N','B']|None=None,pattern_rows:list[str]|None=None,scene:StrictInt|None=None)->dict[str,Any]:
        """Prepare a pattern against a saved layout, without controlling lamps or storing scenes. Provide exactly one built-in8x8 glyph X/M/N/B or explicit matching binary rows. Background must explicitly be off (mapped background level0, color preserved) or preserve (omit background). Output address list is exact, never silently widened to broadcast. Optional scene0..15 is literal; targets are not captured live values. Physical mapping remains user-provided, not independently verified."""
        return service.compose_layout_output(layout_id,foreground_percent,background,kelvin_raw,glyph,pattern_rows,scene)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_capture_project(name:str,channel:StrictInt,addresses:list[StrictInt],scenes:list[StrictInt])->dict[str,Any]:
        """Capture fresh supported configuration fields, groups and stored scenes for explicit1..64 addresses and1..16 scene numbers on one channel. No lighting or device writes. Sequential, NOT an atomic snapshot; full64x16 may take many minutes: keep stdio alive and allow a long request timeout. Journals pending field/scene in a local project record; get_project can inspect it without the bus lock. A saved reading state does not prove the process is alive. Stops partial on failure; incomplete records cannot be imported, diffed or restored. Toolkit JSON format, not ConfigMaster project compatibility. Excludes PC labels, layouts, gateway settings, unimplemented parameters and live output."""
        return service.capture_project(name,channel,addresses,scenes)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_attach_project_layouts(project_id:str,layout_ids:list[str])->dict[str,Any]:
        """Attach1..16 explicitly selected local grid snapshots to a complete project as a NEW immutable revision (schema2, or schema3 when retaining native floorplan evidence). No network or device writes; preserves original capture values/times and any native floorplan. Same configured gateway, firmware and channel; mapped addresses must all be captured. Selected list replaces embedded grids in the NEW revision only. Source human checks remain historical evidence; import does not install active layouts. Checksums detect corruption, not authenticity. Does not convert native floorplans to grids."""
        return service.attach_project_layouts(project_id,layout_ids)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_import_project_layout(project_id:str,source_layout_id:str)->dict[str,Any]:
        """Select one embedded project layout and create a NEW local mapping ID using the existing layout import path. No bus traffic, device changes or physical remapping. Requires current configured gateway/firmware binding; project imports from other gateways are archives only. Clears physical verification/checks on the local mapping, retains source claims in the project. Does not automatically attest wiring or replace existing mappings. Missing/uncaptured layouts are not empty layouts."""
        return service.import_project_layout(project_id,source_layout_id)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_project(project_id:str|None=None)->dict[str,Any]:
        """Export one local project document as structured JSON, or list latest50 with progress/scope. No bus traffic. Completed records have corruption checksums, not authenticity signatures. In-progress records are evidence only, not proof of process liveness. Export may contain private gateway identity and configuration."""
        return service.get_project(project_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_import_project(document:dict[str,Any])->dict[str,Any]:
        """Import a complete schema1/2 toolkit project JSON to a NEW local record. Checks format, completeness, unique scope and checksum, which is not authentication. Schema2 keeps embedded layout snapshots as source evidence only; does not install active mappings or inherit current physical verification. Does NOT restore devices, trust provenance, modify settings or connect to imported network addresses. Other-gateway archives may be imported but cannot be restored to the configured gateway."""
        return service.import_project(document)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_diff_projects(before_id:str,after_id:str)->dict[str,Any]:
        """Compare two complete local projects for the same gateway/IP/firmware/channel. Exact parameter and stored scene values; timestamps/reply metadata ignored. Missing capture coverage is explicitly not_captured, never zero or a deletion instruction. No bus reads/writes. DT6 color bytes are ignored as undefined. Differences do not authorize restoration."""
        return service.diff_projects(before_id,after_id)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_preview_project_scene_restore(project_id:str,addresses:list[StrictInt],scenes:list[StrictInt])->dict[str,Any]:
        """Prepare a READ-ONLY restoration plan for1..64 explicitly selected stored scene pairs from a complete same-gateway project. Fresh capabilities and current original scenes are read first; unsupported types or out-of-current-limit targets reject. Returns the existing expiring scene plan/confirmation. Only an explicit user restore request permits dali_apply_scene_batch through its scoped write gate, fresh conflict check, backup and readback. Does not restore parameters/groups/addressing/PC labels/live lighting; does not write or recall scenes."""
        return service.preview_project_scene_restore(project_id,addresses,scenes)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_preview_project_restore(project_id:str,addresses:list[StrictInt],fields:list[str],group_numbers:list[StrictInt],scenes:list[StrictInt])->dict[str,Any]:
        """READ-ONLY combined toolkit-project restoration preflight for1..64 DT8 addresses, explicit parameter fields0..9, group bits0..15, and scenes0..15 (up to1024 pairs). Empty lists omit that category; at least one required. One serial session reads every selected lamp's full supported parameters/memberships/live and selected scenes before comparing. Checks requested parameter transitions and scene targets against projected limits AFTER selected parameter changes, preserving unselected membership bits. Any unsupported target remains a rejection for the report, never silently dropped into a successful subset. Exact source digest/gateway binding is checked again after capture. Stores originals, differences, pending/error evidence and counts; not atomic, no executable plan/confirmation or device writes. Does NOT restore names/layouts/addressing/gateway settings/live lighting; source provenance remains unverified. Unified execution/rollback is not implemented by this preview."""
        return project_restore.preview(project_id,addresses,fields,group_numbers,scenes)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_project_restore_review(review_id:str)->dict[str,Any]:
        """Read a saved combined project restoration preflight without waiting for the bus lock. Final reports have corruption checksums, not authenticity signatures. Pending/reading records do not prove a process is alive. No network, execution, retry or recovery is triggered."""
        return project_restore.get(review_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_project_restore(project_id:str,addresses:list[StrictInt],fields:list[str],group_numbers:list[StrictInt],scenes:list[StrictInt])->dict[str,Any]:
        """Prepare a fresh source-bound combined toolkit-project restoration plan:1..64 explicit DT8 addresses, selected parameter fields, group bits and scenes0..15. Empty category lists preserve that category. Reads supported parameters/live and selected scenes before planning; checks projected limits and parameter/paired-color order. Stores original evidence and exact steps, not writes. Source checksum is not provenance authentication. Scope excludes PC names, layouts, addressing, gateway settings and live control. Execution is experimental and requires all category gates plus enable_project_restore=true and allowed_project_restore_digest equal to this full plan digest. See release validation for physical scope."""
        return project_execution.prepare(project_id,addresses,fields,group_numbers,scenes)

    @server.tool(annotations=write,structured_output=True)
    @expected_errors
    def dali_start_project_restore(plan_id:str,confirmation:str)->dict[str,Any]:
        """Start an authorized combined restoration in this resident process. Exact digest/category gates, durable one-time claim, all-target fresh conflict check before first write; serial parameters then scenes then memberships, target/live readbacks and final whole selected scope verification. Stop on uncertain receipt, unexpected configuration or live changes; no resend, live controls or automatic rollback. Background task requires keeping this server alive; use get/cancel with SAME plan_id. Other apps/state directories are not locked. Experimental; physical coverage is stated in validation, not implied by successful offline tests."""
        return project_execution.start(plan_id,confirmation)

    @server.tool(annotations=read,structured_output=True)
    @expected_errors
    def dali_get_project_restore(plan_id:str)->dict[str,Any]:
        """Read combined plan/claim/execution and active_in_this_process without waiting for bus lock. Saved writing or pending state is not process liveness. Missing execution after a claim is uncertain, never permission to replay. Returns selected-scope originals and possibly private gateway identity. No network or writes."""
        return project_execution.get(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_cancel_project_restore(plan_id:str)->dict[str,Any]:
        """Request stopping this process's active combined restoration between steps. A sent command completes its readback before cancellation takes effect; poll until inactive and inspect actual attempted steps. Cancellation does not restore prior changes or stop another process."""
        return project_execution.cancel(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_project_restore_undo(plan_id:str)->dict[str,Any]:
        """Fresh conditional undo plan for only the combined execution's attempted prefix. Requires shared OS locks, valid claim/journal and original evidence; current target must equal original or attempted value, and unattempted config/scenes/live must remain unchanged. Refuses third-party drift or missing evidence. Original CT range supports bounded recovery; dependency order is rebuilt so original ranges precede dependent colors/scenes. Embedded original source permits recovery even if external project is missing. Does not execute or blindly reverse steps. Already-original needs no write; otherwise authorize the new plan's exact digest/category scope and start separately."""
        return project_execution.prepare_restore(plan_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False),structured_output=True)
    @expected_errors
    def dali_prepare_workbook_restore(workbook_id:str,channel:StrictInt,addresses:list[StrictInt],fields:list[str],group_numbers:list[StrictInt],scenes:list[StrictInt],scene_source:Literal['ecg','group']|None=None,group:StrictInt|None=None,brightness_policy:Literal['require_unique','native_nearest']='require_unique')->dict[str,Any]:
        """Prepare a combined native XLSX parameter/scene/membership restoration plan for1..64 explicit DT8 addresses. At least one category list must be nonempty; empty lists preserve that category. Selected scenes require explicit ecg or one group source; omit source/group when scenes are empty. Ordinary blank parameters keep current components; scene blanks mean MASK, numeric zero means OFF. Default require_unique rejects lost raw precision; native_nearest explicitly accepts manufacturer percent quantization. Reports conflicts without merging. Embeds bounded original XLSX evidence and exact source decisions separately from fresh device originals, never fabricates a toolkit project/capture. Imported record bytes and configured gateway rechecked before any write. Uses existing project start/get/cancel/conditional undo with identical digest/category gates; undo remains based on bound pre-write originals if external workbook evidence is removed. All-blank parameter-only selection returns no_targets_in_source without network traffic. Does not restore names/layout/address/gateway settings or execute UI import. See validation for physical scope."""
        return project_execution.prepare_workbook(dict(workbook_id=workbook_id,channel=channel,addresses=addresses,fields=fields,group_numbers=group_numbers,scenes=scenes,scene_source=scene_source,group=group,brightness_policy=brightness_policy))

    server.run(transport="stdio")

if __name__ == "__main__":
    main()

