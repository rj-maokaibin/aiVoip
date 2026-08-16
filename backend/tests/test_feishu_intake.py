from app.integrations.feishu.intake import extract_message_content, route_intake


def test_device_url_without_symptom_requires_clarification_and_does_not_require_access():
    result = route_intake(text='打开SSH sn=SN-1 ip=10.0.0.1')
    assert result.intent == 'NEW_DIAGNOSIS'
    assert 'symptom_description' in result.missing_user_inputs
    assert result.requires_device_access is False


def test_symptom_and_device_routes_new_diagnosis_with_access():
    result = route_intake(text='设备单通无声，请排查 sn=SN-1 ip=10.0.0.1')
    assert result.intent == 'NEW_DIAGNOSIS'
    assert result.missing_user_inputs == []
    assert result.requires_device_access is True


def test_attachment_is_evidence_first_and_never_requires_device_access():
    attachment = {'file_key': 'file-1', 'filename': 'call.pcap'}
    result = route_intake(text='', attachments=[attachment])
    assert result.intent == 'NEW_DIAGNOSIS'
    assert result.requires_device_access is False
    assert 'device_url_or_ip_and_sn_or_attachment' not in result.missing_user_inputs


def test_stop_and_status_are_not_diagnosis_provision_routes():
    assert route_intake(text='停止复现').intent == 'STOP_REPRODUCTION'
    status = route_intake(text='现在进度怎么样？', has_thread_case=False)
    assert status.intent == 'STATUS_QUERY'
    assert status.missing_user_inputs


def test_file_message_content_extracts_attachment():
    text, attachments = extract_message_content({
        'message_type': 'file',
        'content': '{"file_key":"fk-1","file_name":"call.pcapng"}',
    })
    assert text == ''
    assert attachments == [{
        'file_key': 'fk-1', 'filename': 'call.pcapng', 'message_type': 'file',
        'resource_type': 'file',
    }]


def test_post_extracts_text_and_images():
    text, attachments = extract_message_content({
        'message_type': 'post',
        'content': '{"zh_cn":{"content":[[{"tag":"text","text":"单通无声"},{"tag":"img","image_key":"img-1"}]]}}',
    })
    assert text == '单通无声'
    assert attachments[0]['file_key'] == 'img-1'
