from typing import Dict, List, NamedTuple
import pykube.objects
import requests
import pendulum
import boto3
from urllib.parse import urlparse
from ..context import Context
from k8s_snapshots.snapshot import Snapshot
from .abstract import NewSnapshotIdentifier, SnapshotStatus
from ..errors import SnapshotCreateError


def validate_config(config):
    """Ensure the config of this backend is correct.

    manual volumes are validated by the backend
        - for aws, google cloud, need different data, say, region or zone.
    """
    pass


def supports_volume(volume: pykube.objects.PersistentVolume):
    return bool(volume.obj['spec'].get('awsElasticBlockStore'))


class AWSDiskIdentifier(NamedTuple):
    region: str
    volume_id: str


def get_current_region(ctx):
    """Get the current region from the metadata service.
    """
    if not ctx.config['aws_region']:
        response = requests.get(
            'http://169.254.169.254/latest/meta-data/placement/availability-zone',
            timeout=5)
        response.raise_for_status()
        ctx.config['aws_region'] = response.text[:-1]

    return ctx.config['aws_region']



def get_disk_identifier(volume: pykube.objects.PersistentVolume):
    volume_url = volume.obj['spec'].get('awsElasticBlockStore')['volumeID']

    if volume_url.startswith('aws://'):
        # An url such as aws://eu-west-1a/vol-00292b2da3d4ed1e4
        parts = urlparse(volume_url)
        zone = parts.netloc
        volume_id = parts.path[1:]

        return AWSDiskIdentifier(region=zone[:-1], volume_id=volume_id)
    else:
        # Older versions of kube just put the volume id in the volume id field.
        volume_id = volume_url
        region = volume.obj['metadata']['labels']['failure-domain.beta.kubernetes.io/region']
        return AWSDiskIdentifier(region=region, volume_id=volume_id)

def parse_timestamp(date) -> pendulum.Pendulum:
    return pendulum.instance(date)


def validate_disk_identifier(disk_id: Dict):
    try:
        return AWSDiskIdentifier(
            region=disk_id['region'],
            volume_id=disk_id['volumeId']
        )
    except:
        raise ValueError(disk_id)

# AWS can filter by volume-id, which means we wouldn't have to match in Python.
# In any case, it might be easier to let the backend handle the matching. Then
# it relies less on the DiskIdentifier object always matching.
#filters={'volume-id': volume.id}
def load_snapshots(ctx: Context, label_filters: Dict[str, str]) -> List[Snapshot]:
    connection = get_connection(ctx, region=get_current_region(ctx))

    snapshots = connection.describe_snapshots(
        OwnerIds=['self'],
        Filters=[{'Name': f'tag:{k}', 'Values': [v]} for k, v in label_filters.items()]
    )

    return list(map(lambda snapshot: Snapshot(
        name=snapshot['SnapshotId'],
        created_at=parse_timestamp(snapshot['StartTime']),
        disk=AWSDiskIdentifier(
            volume_id=snapshot['VolumeId'],
            region=ctx.config['aws_region']
        )
    ), snapshots['Snapshots']))


def create_snapshot(
    ctx: Context,
    disk: AWSDiskIdentifier,
    snapshot_name: str,
    snapshot_description: str
) -> NewSnapshotIdentifier:

    connection = get_connection(ctx, disk.region)

    # TODO: Seems like the API doesn't actually allow us to set a snapshot
    # name, although it's possible in the UI.
    snapshot = connection.create_snapshot(
        VolumeId=disk.volume_id,
        Description=snapshot_name
    )
    
    return {
        'id': snapshot['SnapshotId'],
        'region': disk.region
    }


def get_snapshot_status(
    ctx: Context,
    snapshot_identifier: NewSnapshotIdentifier
) -> SnapshotStatus:
    connection = get_connection(ctx, snapshot_identifier['region'])

    snapshots = connection.describe_snapshots(
        SnapshotIds=[snapshot_identifier['id']]
    )
    snapshot = snapshots['Snapshots'][0]
    
    # Can be pending | completed | error
    if snapshot['State'] == 'pending':
        return SnapshotStatus.PENDING
    elif snapshot['State'] == 'completed':
        return SnapshotStatus.COMPLETE
    elif snapshot['State'] == 'error':
        raise SnapshotCreateError(snapshot['status'])
    else:
        raise NotImplementedError()


def set_snapshot_labels(
    ctx: Context,
    snapshot_identifier: NewSnapshotIdentifier,
    labels: Dict
):
    connection = get_connection(ctx, snapshot_identifier['region'])
    connection.create_tags(
        Resources=[snapshot_identifier['id']],
        Tags=[{'Key': k, 'Value': v} for k, v in labels.items()]
    )


def delete_snapshot(
    ctx: Context,
    snapshot: Snapshot
):
    connection = get_connection(ctx, snapshot.disk.region)
    connection.delete_snapshot(SnapshotId=snapshot.name)


def get_connection(ctx: Context, region):
    connection = boto3.client('ec2', region_name=region)
    return connection
